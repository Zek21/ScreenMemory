#!/usr/bin/env python3
"""
skynet_self_prompt.py -- Orchestrator heartbeat daemon.

Monitors bus, workers, and TODOs. When the orchestrator goes silent and
there's work to do, auto-types a status summary into the orchestrator's
chat window so it wakes up and acts.

This is the HEARTBEAT of Skynet. Without it, the orchestrator sleeps
between user messages and workers go idle.

Usage:
    python tools/skynet_self_prompt.py start   # run as daemon
    python tools/skynet_self_prompt.py status   # show last prompt info
    python tools/skynet_self_prompt.py once     # single check, prompt if needed
    python tools/skynet_self_prompt.py stop     # show PID for manual stop
"""

import json
import hashlib
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

DATA_DIR = ROOT / "data"
PID_FILE = DATA_DIR / "self_prompt.pid"
LOG_FILE = DATA_DIR / "self_prompt_log.json"
LAST_ACTION_FILE = DATA_DIR / "last_orchestrator_action.json"
ORCH_FILE = DATA_DIR / "orchestrator.json"
WORKERS_FILE = DATA_DIR / "workers.json"
TODOS_FILE = DATA_DIR / "todos.json"
ORCH_QUEUE_FILE = DATA_DIR / "orch_queue.json"
BRAIN_CONFIG_FILE = DATA_DIR / "brain_config.json"
DISPATCH_LOCK_FILE = DATA_DIR / "dispatch_active.lock"
BOOT_IN_PROGRESS_FILE = DATA_DIR / "boot_in_progress.json"
HEALTH_FILE = DATA_DIR / "self_prompt_health.json"
DELIVERED_FILE = DATA_DIR / "self_prompt_delivered.json"
BUS_URL = "http://localhost:8420"

# Defaults (overridden by brain_config.json -> self_prompt section)
LOOP_INTERVAL = 300
MIN_PROMPT_GAP = 45
PROMPT_THRESHOLD = 50
IDLE_WORKER_THRESHOLD = 90    # raised: 30s was too aggressive, workers need time
ORCH_INACTIVE_THRESHOLD = 45
MAX_LOG_ENTRIES = 50
HEALTH_REPORT_INTERVAL = 300  # report health to bus every 5 min
MAX_CONSECUTIVE_PROMPTS = 3   # stop after N prompts without orchestrator action

ALL_IDLE_INTERVAL = 60  # faster prompting when all workers idle

def _load_config_overrides():
    """Load self_prompt thresholds from brain_config.json. Called on startup AND each cycle (hot-reload)."""
    global LOOP_INTERVAL, MIN_PROMPT_GAP, IDLE_WORKER_THRESHOLD
    global ORCH_INACTIVE_THRESHOLD, HEALTH_REPORT_INTERVAL, PROMPT_THRESHOLD, MAX_CONSECUTIVE_PROMPTS
    global ALL_IDLE_INTERVAL
    try:
        cfg = json.loads(BRAIN_CONFIG_FILE.read_text(encoding="utf-8"))
        sp = cfg.get("self_prompt", {})
        if sp.get("loop_interval"):
            LOOP_INTERVAL = sp["loop_interval"]
        if sp.get("min_prompt_gap"):
            MIN_PROMPT_GAP = sp["min_prompt_gap"]
        if sp.get("prompt_threshold"):
            PROMPT_THRESHOLD = sp["prompt_threshold"]
        if sp.get("idle_worker_threshold"):
            IDLE_WORKER_THRESHOLD = sp["idle_worker_threshold"]
        if sp.get("orch_inactive_threshold"):
            ORCH_INACTIVE_THRESHOLD = sp["orch_inactive_threshold"]
        if sp.get("health_report_interval"):
            HEALTH_REPORT_INTERVAL = sp["health_report_interval"]
        if sp.get("max_consecutive"):
            MAX_CONSECUTIVE_PROMPTS = sp["max_consecutive"]
        if sp.get("all_idle_interval"):
            ALL_IDLE_INTERVAL = sp["all_idle_interval"]
    except Exception:
        pass

_load_config_overrides()

import ctypes
import ctypes.wintypes
user32 = ctypes.windll.user32

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _hidden_subprocess_kwargs(**kwargs):
    merged = dict(kwargs)
    if sys.platform == "win32":
        merged["creationflags"] = merged.get("creationflags", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = merged.get("startupinfo")
        if startupinfo is None and hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            merged["startupinfo"] = startupinfo
    return merged


def _hidden_check_output(args, **kwargs):
    return subprocess.check_output(args, **_hidden_subprocess_kwargs(**kwargs))


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [SELF-PROMPT] [{level}] {msg}", flush=True)


def _fetch_json(url, timeout=5):
    try:
        return json.loads(urllib.request.urlopen(url, timeout=timeout).read())
    except Exception:
        return None


def _post_bus(topic, msg_type, content):
    try:
        payload = json.dumps({
            "sender": "self_prompt",
            "topic": topic,
            "type": msg_type,
            "content": content,
        }).encode()
        req = urllib.request.Request(
            f"{BUS_URL}/bus/publish", payload,
            {"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _get_worker_state(hwnd):
    """Get worker state via UIA engine (uses singleton)."""
    try:
        from uia_engine import get_engine
        result = get_engine().scan(int(hwnd))
        return getattr(result, "state", "UNKNOWN")
    except Exception:
        return "UNKNOWN"


def _is_window_alive(hwnd):
    return bool(user32.IsWindow(int(hwnd)))


def _load_workers():
    data = _load_json(WORKERS_FILE)
    if not data:
        return []
    workers = data.get("workers", [])
    return workers if isinstance(workers, list) else []


def _get_orch_hwnd():
    data = _load_json(ORCH_FILE)
    if data:
        return data.get("orchestrator_hwnd", 0)
    return 0


def _update_last_action(reason="prompt_sent"):
    """Record that orchestrator was active."""
    _save_json(LAST_ACTION_FILE, {
        "timestamp": datetime.now().isoformat(),
        "reason": reason,
        "t": time.time(),
    })


def _get_last_action_age():
    """Seconds since orchestrator last acted. Returns float('inf') if unknown."""
    data = _load_json(LAST_ACTION_FILE)
    if data and "t" in data:
        return time.time() - data["t"]
    return float("inf")


def _get_pending_todos(worker=None):
    """Count pending/active TODOs."""
    return len(_get_pending_todo_items(worker))


def _get_pending_todo_items(worker=None, limit=None):
    """Return pending/active TODO items sorted by priority then creation time."""
    data = _load_json(TODOS_FILE)
    if not data:
        return []
    # Handle both list format and dict-with-todos-key format
    todo_list = data if isinstance(data, list) else data.get("todos", [])
    items = [t for t in todo_list if isinstance(t, dict) and t.get("status") in ("pending", "active")]
    if worker:
        items = [t for t in items if t.get("worker") == worker]
    priority_rank = {"critical": 0, "high": 1, "normal": 2, "low": 3}
    items.sort(key=lambda t: (
        priority_rank.get(str(t.get("priority", "normal")).lower(), 9),
        str(t.get("created_at", "")),
        str(t.get("id", "")),
    ))
    if limit is not None:
        return items[:limit]
    return items


TASK_FILE = DATA_DIR / "task_queue.json"

def _get_pending_tasks(worker=None):
    """Count pending tasks from task_queue.json (Zero Ticket Stop)."""
    data = _load_json(TASK_FILE)
    if not data:
        return 0
    tasks = data.get("tasks", [])
    terminal = ("done", "failed", "cancelled")
    if worker:
        tasks = [t for t in tasks if t.get("target") in (worker, "all")]
    return sum(1 for t in tasks if t.get("status") not in terminal)


def _append_log(entry):
    """Append to prompt log (capped)."""
    log_data = _load_json(LOG_FILE) or []
    if not isinstance(log_data, list):
        log_data = []
    log_data.append(entry)
    if len(log_data) > MAX_LOG_ENTRIES:
        log_data = log_data[-MAX_LOG_ENTRIES:]
    _save_json(LOG_FILE, log_data)


def _send_self_prompt(orch_hwnd, prompt_text):
    """Type a prompt into the orchestrator's chat window."""
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        from skynet_dispatch import ghost_type_to_worker
        # For self-prompt, target=orchestrator, orch_hwnd=orchestrator (focus returns to self)
        ok = ghost_type_to_worker(orch_hwnd, prompt_text, orch_hwnd)
        return ok
    except Exception as e:
        log(f"Send failed: {e}", "ERROR")
        return False


DAEMON_STATE_FILE = DATA_DIR / "daemon_state.json"

# ── Temporal Event Types ────────────────────────────────────────────────────
EVT_RESULT     = "result"
EVT_ALERT      = "alert"
EVT_DISPATCH   = "dispatch"
EVT_IDLE_START = "idle_start"
EVT_IDLE_END   = "idle_end"
EVT_FAILURE    = "failure"
EVT_STALL      = "stall"
EVT_PROMPT     = "prompt"
EVT_STATE_CHG  = "state_change"


class TemporalEvent:
    """Timestamped event for pattern analysis."""
    __slots__ = ("t", "kind", "worker", "data")
    def __init__(self, kind, worker="system", data=""):
        self.t = time.time()
        self.kind = kind
        self.worker = worker
        self.data = str(data)[:200]

    def age(self):
        return time.time() - self.t

    def to_dict(self):
        return {"t": self.t, "kind": self.kind, "worker": self.worker, "data": self.data}


class WorkerCognitiveModel:
    """Real-time cognitive profile for a single worker."""
    __slots__ = (
        "name", "state", "state_since", "transitions", "tasks_completed",
        "tasks_failed", "avg_task_duration", "specializations",
        "last_result_quality", "cognitive_load", "stall_count",
    )

    def __init__(self, name, specializations=None):
        self.name = name
        self.state = "UNKNOWN"
        self.state_since = time.time()
        self.transitions = []      # last 20 (from_state, to_state, duration)
        self.tasks_completed = 0
        self.tasks_failed = 0
        self.avg_task_duration = 0.0
        self.specializations = specializations or []
        self.last_result_quality = "unknown"  # "success", "partial", "failure"
        self.cognitive_load = 0.0   # 0.0 = idle, 1.0 = saturated
        self.stall_count = 0

    def update_state(self, new_state):
        if new_state == self.state:
            return False
        dur = time.time() - self.state_since
        self.transitions.append((self.state, new_state, dur))
        if len(self.transitions) > 20:
            self.transitions = self.transitions[-20:]
        # Update cognitive load based on state
        if new_state == "IDLE":
            self.cognitive_load = max(0.0, self.cognitive_load - 0.3)
        elif new_state == "PROCESSING":
            self.cognitive_load = min(1.0, self.cognitive_load + 0.2)
        elif new_state == "STEERING":
            self.cognitive_load = 0.8  # stuck in steering = high load, no output
            self.stall_count += 1
        old = self.state
        self.state = new_state
        self.state_since = time.time()
        return True  # state changed

    def avg_processing_time(self):
        proc_durs = [d for (f, t, d) in self.transitions if f == "PROCESSING" and t == "IDLE"]
        return sum(proc_durs) / len(proc_durs) if proc_durs else 0.0

    def idle_duration(self):
        return (time.time() - self.state_since) if self.state == "IDLE" else 0.0

    def effectiveness_score(self):
        total = self.tasks_completed + self.tasks_failed
        if total == 0:
            return 0.5
        return self.tasks_completed / total

    def to_dict(self):
        return {
            "name": self.name, "state": self.state,
            "idle_s": round(self.idle_duration()),
            "tasks": self.tasks_completed, "fails": self.tasks_failed,
            "avg_proc_s": round(self.avg_processing_time()),
            "load": round(self.cognitive_load, 2),
            "effectiveness": round(self.effectiveness_score(), 2),
            "stalls": self.stall_count,
        }


class ChainOfThought:
    """Multi-step reasoning engine that produces structured thoughts."""

    def __init__(self):
        self.steps = []

    def think(self, observation, reasoning, conclusion):
        self.steps.append({
            "observation": observation,
            "reasoning": reasoning,
            "conclusion": conclusion,
            "t": time.time(),
        })

    def has_conclusions(self):
        return len(self.steps) > 0

    def summary(self, max_steps=5):
        recent = self.steps[-max_steps:]
        parts = []
        for i, s in enumerate(recent, 1):
            parts.append(f"T{i}:{s['conclusion']}")
        return " -> ".join(parts)

    def critical_conclusions(self):
        return [s["conclusion"] for s in self.steps if "CRITICAL" in s.get("conclusion", "").upper() or "URGENT" in s.get("conclusion", "").upper()]

    def reset(self):
        self.steps = []


class SelfPromptDaemon:
    """SKYNET CONSCIOUSNESS KERNEL -- Chain-of-Thought Self-Prompt Daemon.

    The supreme intelligence layer. This daemon doesn't just monitor -- it THINKS.
    Every cycle runs a multi-step chain of thought:
      1. PERCEIVE  -- scan all worker states, bus messages, system health
      2. REMEMBER  -- consult temporal event history for patterns
      3. REASON    -- chain-of-thought analysis of observations
      4. PREDICT   -- anticipate future states and bottlenecks
      5. DECIDE    -- determine if orchestrator needs activation
      6. ACT       -- deliver precisely crafted intelligence briefing
      7. LEARN     -- record outcomes for self-calibration
    """

    def __init__(self):
        # Core state
        self.last_prompt_time = 0.0
        self.last_health_report = 0.0
        self.seen_result_ids = set()
        self.seen_alert_ids = set()
        self.accumulated_score = 0
        self.last_prompt_hash = ""
        self.last_prompt_hash_time = 0.0
        self.prompts_sent = 0
        self.prompts_failed = 0
        self.cycles = 0
        self._boot_done = False

        # Restore delivered IDs from persistent file to avoid re-delivering after restart
        delivered = _load_json(DELIVERED_FILE)
        if delivered and isinstance(delivered, dict):
            self.seen_result_ids = set(delivered.get("result", []))
            self.seen_alert_ids = set(delivered.get("alert", []))

        # ── CONSECUTIVE PROMPT LIMITER ──
        self.consecutive_prompts = 0
        self.max_consecutive = MAX_CONSECUTIVE_PROMPTS
        self.last_dispatch_check_count = 0

        # ── INTELLIGENCE SUBSYSTEMS ──
        # Temporal Pattern Engine: sliding window of events
        self.event_history = []          # list of TemporalEvent (last 500)
        self.EVENT_WINDOW = 500

        # Worker Cognitive Models: real-time cognitive profile per worker
        self.worker_models = {}          # name -> WorkerCognitiveModel

        # Mission Continuity: track active missions across prompts
        self.active_missions = []        # list of {id, goal, dispatched_to, started, status}
        self.mission_counter = 0

        # Self-Calibration: effectiveness tracking
        self.prompt_effectiveness = []   # last 50 (prompt_time, was_acted_on)
        self.dynamic_threshold = PROMPT_THRESHOLD
        self.dynamic_gap = MIN_PROMPT_GAP

        # Chain of Thought: per-cycle reasoning
        self.cot = ChainOfThought()

        # Anomaly Detection: baseline metrics
        self.baseline_dispatch_rate = 0.0  # dispatches per hour
        self.baseline_completion_rate = 0.0
        self.anomaly_alerts = []

        # ── PLANNER INTEGRATION (Level 4) ──
        # HierarchicalPlanner for autonomous mission generation
        self._planner = None
        self._last_mission_plan_time = 0.0
        self._mission_plan_cooldown = 300  # generate at most 1 mission plan per 5 min
        self._mission_plan_history = []     # last 20 generated plans
        self._init_planner()

        # Load persistent state
        self._load_persistent_state()

    def _load_persistent_state(self):
        """Restore temporal memory from disk."""
        state = _load_json(DAEMON_STATE_FILE)
        if state:
            self.prompts_sent = state.get("prompts_sent", 0)
            self.prompts_failed = state.get("prompts_failed", 0)
            self.dynamic_threshold = state.get("dynamic_threshold", PROMPT_THRESHOLD)
            self.dynamic_gap = state.get("dynamic_gap", MIN_PROMPT_GAP)
            self.mission_counter = state.get("mission_counter", 0)
            # Restore missions
            for m in state.get("active_missions", []):
                if m.get("status") not in ("completed", "failed"):
                    self.active_missions.append(m)
            log(f"Restored state: {self.prompts_sent} prompts, threshold={self.dynamic_threshold}, "
                f"{len(self.active_missions)} active missions")

    def _save_persistent_state(self):
        """Persist key state to disk for crash recovery."""
        state = {
            "prompts_sent": self.prompts_sent,
            "prompts_failed": self.prompts_failed,
            "dynamic_threshold": self.dynamic_threshold,
            "dynamic_gap": self.dynamic_gap,
            "mission_counter": self.mission_counter,
            "active_missions": self.active_missions[-20:],
            "worker_models": {n: m.to_dict() for n, m in self.worker_models.items()},
            "anomaly_count": len(self.anomaly_alerts),
            "saved_at": datetime.now().isoformat(),
        }
        _save_json(DAEMON_STATE_FILE, state)

    def _init_planner(self):
        """Initialize the HierarchicalPlanner for autonomous mission generation."""
        try:
            from core.cognitive.planner import HierarchicalPlanner
            self._planner = HierarchicalPlanner()
            log("Planner initialized (Level 4: autonomous mission generation)")
        except Exception as e:
            log(f"Planner init failed (missions will use fallback): {e}", "WARN")
            self._planner = None

    def _plan_autonomous_mission(self, perception, patterns):
        """Use HierarchicalPlanner to generate an autonomous improvement mission.
        
        Called when workers are idle and no pending work exists.
        Analyzes system state to identify the highest-impact improvement goal,
        decomposes it into subtasks, and returns a dispatch-ready mission.
        
        Returns: (mission_goal, subtask_descriptions) or (None, None) if no mission needed.
        """
        now = time.time()
        
        # Cooldown: don't plan missions too frequently
        if now - self._last_mission_plan_time < self._mission_plan_cooldown:
            return None, None

        # Only plan when workers are idle AND no pending work
        idle_workers = [n for n, i in perception["workers"].items() 
                       if i.get("state") == "IDLE" and i.get("alive")]
        if not idle_workers:
            return None, None

        pending = perception.get("pending_todos", 0) + perception.get("pending_tasks", 0)
        if pending > 0:
            return None, None  # there's already work to do, no need to generate more

        # Analyze system state to determine best improvement goal
        goal = self._identify_improvement_goal(perception, patterns)
        if not goal:
            return None, None

        # Use Planner to decompose goal into subtasks
        if self._planner:
            try:
                plan = self._planner.create_plan(goal, context=self._build_planning_context(perception))
                subtask_descriptions = [st.description for st in plan.subtasks]
                
                self._last_mission_plan_time = now
                self.mission_counter += 1
                
                mission = {
                    "id": f"mission_{self.mission_counter}",
                    "goal": goal,
                    "subtasks": subtask_descriptions,
                    "dispatched_to": idle_workers[:len(subtask_descriptions)],
                    "started": now,
                    "status": "planned",
                    "source": "planner",
                }
                self.active_missions.append(mission)
                self._mission_plan_history.append({
                    "goal": goal,
                    "subtasks": len(subtask_descriptions),
                    "time": datetime.now().isoformat(),
                })
                if len(self._mission_plan_history) > 20:
                    self._mission_plan_history = self._mission_plan_history[-20:]

                log(f"PLANNER: Generated mission '{goal}' with {len(subtask_descriptions)} subtasks")
                return goal, subtask_descriptions
            except Exception as e:
                log(f"Planner decomposition failed: {e}", "WARN")

        # Fallback: return the raw goal without decomposition
        self._last_mission_plan_time = now
        return goal, [goal]

    def _identify_improvement_goal(self, perception, patterns) -> str:
        """Analyze system state to identify the highest-impact improvement goal.
        
        Priority order:
        1. Fix failures: if recent failures detected, investigate root cause
        2. Activate dormant engines: if engines are available but not online
        3. Improve low-IQ metrics: if IQ trending down
        4. Wire unused cognitive modules: GoT, MCTS, Reflexion
        5. General self-improvement: codebase audit, test coverage, docs
        """
        # Priority 1: Recent failures need investigation
        if patterns.get("failure_rate_10m", 0) >= 2:
            return "Investigate and fix recent task failures -- check worker logs, bus error messages, and dispatch pipeline"

        # Priority 2: Dormant engines (available but not online)
        engine_status = perception.get("engine_status")
        if engine_status and isinstance(engine_status, dict):
            engines = engine_status.get("engines", {})
            available_but_offline = [
                name for name, info in engines.items()
                if isinstance(info, dict) and info.get("status") == "available"
            ]
            if available_but_offline:
                target = available_but_offline[0]
                return f"Activate dormant engine '{target}' -- investigate why it is 'available' but not 'online', fix dependencies"

        # Priority 3: IQ trend analysis
        iq_data = perception.get("iq")
        if iq_data and isinstance(iq_data, dict):
            iq_val = iq_data.get("iq", 0)
            if iq_val < 0.80:
                return f"Improve collective IQ (currently {iq_val:.4f}) -- audit low-scoring engines, add missing capabilities, improve test coverage"

        # Priority 4: Wire cognitive modules
        cognitive_goals = [
            "Wire core/cognitive/reflexion.py into the dispatch pipeline -- after task failures, auto-generate verbal self-critiques and store in learning_store",
            "Wire core/cognitive/graph_of_thoughts.py into skynet_brain.py -- use GoT for complex multi-branch reasoning on COMPLEX/ADVERSARIAL tasks",
            "Wire core/cognitive/knowledge_distill.py as a background daemon -- consolidate decaying episodic memories into durable semantic knowledge",
            "Wire core/cognitive/mcts.py into web navigation tasks -- use R-MCTS for autonomous browser interaction planning",
        ]
        # Pick one that hasn't been planned recently
        recent_goals = {m.get("goal", "") for m in self._mission_plan_history[-10:]}
        for g in cognitive_goals:
            if g not in recent_goals:
                return g

        # Priority 5: General improvement
        general_goals = [
            "Audit and improve test coverage for tools/skynet_*.py -- identify untested functions and add targeted tests",
            "Review and optimize the dispatch pipeline -- measure latency, identify bottlenecks, reduce overhead",
            "Update AGENTS.md documentation to reflect Level 4 capabilities and new features",
            "Scan codebase for TODO/FIXME/HACK comments and create actionable improvement tickets",
            "Profile and optimize the UIA engine scan performance -- target sub-100ms per scan",
        ]
        for g in general_goals:
            if g not in recent_goals:
                return g

        return ""

    def _build_planning_context(self, perception) -> str:
        """Build context string for the Planner from current system state."""
        parts = []

        # Worker availability
        idle = [n for n, i in perception["workers"].items() if i.get("state") == "IDLE"]
        parts.append(f"Available workers: {', '.join(idle)}")

        # IQ
        iq = perception.get("iq")
        if iq and isinstance(iq, dict):
            parts.append(f"Current IQ: {iq.get('iq', 'unknown')}")

        # Engine status summary
        es = perception.get("engine_status")
        if es and isinstance(es, dict):
            engines = es.get("engines", {})
            online = sum(1 for e in engines.values() if isinstance(e, dict) and e.get("status") == "online")
            total = len(engines)
            parts.append(f"Engines: {online}/{total} online")

        # Recent activity
        parts.append(f"Pending TODOs: {perception.get('pending_todos', 0)}")
        parts.append(f"Pending tasks: {perception.get('pending_tasks', 0)}")

        return "; ".join(parts)

    def _orchestrator_took_action(self):
        """Check if orchestrator dispatched something new since last prompt.
        Returns True if new orchestrator action detected (resets consecutive counter)."""
        # Check dispatch_log.json entry count
        dispatch_log = _load_json(DATA_DIR / "dispatch_log.json")
        if dispatch_log:
            entries = dispatch_log if isinstance(dispatch_log, list) else dispatch_log.get("log", [])
            current_count = len(entries)
            if current_count > self.last_dispatch_check_count:
                self.last_dispatch_check_count = current_count
                return True

        # Check bus for directives not from self_prompt since last prompt
        try:
            url = f"{BUS_URL}/bus/messages?limit=20"
            msgs = _fetch_json(url)
            if msgs:
                cutoff = self.last_prompt_time
                for m in (msgs if isinstance(msgs, list) else []):
                    ts = m.get("timestamp", "")
                    sender = m.get("sender", "")
                    mtype = m.get("type", "")
                    if sender == "self_prompt":
                        continue
                    if mtype == "directive" and ts:
                        try:
                            msg_time = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                            if msg_time > cutoff:
                                return True
                        except Exception:
                            pass
        except Exception:
            pass

        return False

    def _record_event(self, kind, worker="system", data=""):
        """Append to temporal event history."""
        evt = TemporalEvent(kind, worker, data)
        self.event_history.append(evt)
        if len(self.event_history) > self.EVENT_WINDOW:
            self.event_history = self.event_history[-self.EVENT_WINDOW:]

    def _events_in_window(self, kind=None, window_s=300, worker=None):
        """Count events of a type in the last N seconds."""
        cutoff = time.time() - window_s
        return [e for e in self.event_history
                if e.t >= cutoff
                and (kind is None or e.kind == kind)
                and (worker is None or e.worker == worker)]

    # ── PHASE 1: PERCEIVE ───────────────────────────────────────────────────

    def _perceive(self):
        """Scan the entire system state. Returns structured perception."""
        perception = {
            "workers": {},
            "bus_results": [],
            "bus_alerts": [],
            "pending_todos": 0,
            "pending_tasks": 0,
            "profiles": {},
            "skynet_status": None,
            "engine_status": None,
            "iq": None,
            "learning_store": None,
        }

        # Worker states via UIA
        workers = _load_workers()
        for w in workers:
            name = w.get("name", "?")
            hwnd = w.get("hwnd", 0)
            alive = hwnd and _is_window_alive(hwnd)
            state = "DEAD"
            if alive:
                state = _get_worker_state(hwnd)

            # Initialize or update cognitive model
            if name not in self.worker_models:
                specs = []
                try:
                    pdata = _load_json(DATA_DIR / "agent_profiles.json") or {}
                    specs = pdata.get(name, {}).get("specializations", [])
                except Exception:
                    pass
                self.worker_models[name] = WorkerCognitiveModel(name, specs)

            model = self.worker_models[name]
            changed = model.update_state(state)
            if changed:
                self._record_event(EVT_STATE_CHG, name, f"{model.transitions[-1][0]}->{state}")

            perception["workers"][name] = {
                "state": state, "alive": alive, "hwnd": hwnd,
                "model": model,
            }

        # Bus messages
        bus_msgs = _fetch_json(f"{BUS_URL}/bus/messages?limit=50")
        if bus_msgs and isinstance(bus_msgs, list):
            for m in bus_msgs:
                mid = m.get("id", "")
                msg_type = m.get("type", "")
                msg_topic = m.get("topic", "")

                # Results addressed to orchestrator
                if msg_type == "result" and msg_topic == "orchestrator":
                    if mid and mid not in self.seen_result_ids:
                        self.seen_result_ids.add(mid)
                        perception["bus_results"].append(m)
                        self._record_event(EVT_RESULT, m.get("sender", "?"), str(m.get("content", ""))[:100])
                        # Update worker cognitive model
                        sender = m.get("sender", "")
                        if sender in self.worker_models:
                            content_lower = str(m.get("content", "")).lower()
                            if any(kw in content_lower for kw in ("error", "failed", "timeout")):
                                self.worker_models[sender].tasks_failed += 1
                                self.worker_models[sender].last_result_quality = "failure"
                                self._record_event(EVT_FAILURE, sender, content_lower[:80])
                            else:
                                self.worker_models[sender].tasks_completed += 1
                                self.worker_models[sender].last_result_quality = "success"

                # Alerts (all types including urgent)
                if msg_type in ("alert", "monitor_alert", "service_alert", "urgent"):
                    if mid and mid not in self.seen_alert_ids:
                        self.seen_alert_ids.add(mid)
                        perception["bus_alerts"].append(m)
                        self._record_event(EVT_ALERT, m.get("sender", "?"), str(m.get("content", ""))[:80])

        # Pending work
        perception["pending_todos"] = _get_pending_todos()
        perception["pending_tasks"] = _get_pending_tasks()

        # Agent profiles
        try:
            pdata = _load_json(DATA_DIR / "agent_profiles.json") or {}
            for k, v in pdata.items():
                if isinstance(v, dict) and k not in ("version", "updated_at", "updated_by"):
                    perception["profiles"][k] = v
        except Exception:
            pass

        # Skynet backend status
        perception["skynet_status"] = _fetch_json(f"{BUS_URL}/status")

        # Engine health
        perception["engine_status"] = _fetch_json("http://localhost:8421/engines")

        # IQ
        try:
            iq_data = _load_json(DATA_DIR / "iq_history.json") or {}
            h = iq_data.get("history", [])
            if h:
                perception["iq"] = h[-1]
        except Exception:
            pass

        # Learning store
        try:
            from core.learning_store import LearningStore
            ls = LearningStore()
            perception["learning_store"] = ls.stats()
        except Exception:
            pass

        return perception

    # ── PHASE 2: REMEMBER (Temporal Pattern Analysis) ───────────────────────

    def _remember(self, perception):
        """Analyze temporal patterns from event history."""
        patterns = {
            "result_rate_5m": 0,    # results in last 5 min
            "alert_rate_5m": 0,     # alerts in last 5 min
            "failure_rate_10m": 0,  # failures in last 10 min
            "state_changes_5m": 0,  # worker state changes in last 5 min
            "idle_streak_workers": [],  # workers idle >2min
            "stall_pattern": False,     # repeated idle->processing->idle without results
            "dispatch_drought": False,  # no dispatches in 10 min
            "convergence": [],          # workers reporting on same topics
        }

        patterns["result_rate_5m"] = len(self._events_in_window(EVT_RESULT, 300))
        patterns["alert_rate_5m"] = len(self._events_in_window(EVT_ALERT, 300))
        patterns["failure_rate_10m"] = len(self._events_in_window(EVT_FAILURE, 600))
        patterns["state_changes_5m"] = len(self._events_in_window(EVT_STATE_CHG, 300))
        patterns["dispatch_drought"] = len(self._events_in_window(EVT_DISPATCH, 600)) == 0

        # Idle streak detection
        for name, info in perception["workers"].items():
            model = info.get("model")
            if model and model.idle_duration() > 120:
                patterns["idle_streak_workers"].append(name)

        # Stall pattern: worker cycles between states without producing results
        for name, model in self.worker_models.items():
            if len(model.transitions) >= 4:
                last4 = model.transitions[-4:]
                states = [t[1] for t in last4]
                if states.count("IDLE") >= 2 and states.count("PROCESSING") >= 1:
                    recent_results = self._events_in_window(EVT_RESULT, 300, worker=name)
                    if len(recent_results) == 0:
                        patterns["stall_pattern"] = True

        # Convergence detection: multiple workers reporting similar content
        recent_results = self._events_in_window(EVT_RESULT, 600)
        if len(recent_results) >= 2:
            word_map = {}
            for e in recent_results:
                words = set(w.lower() for w in e.data.split() if len(w) > 4)
                for w in words:
                    word_map.setdefault(w, set()).add(e.worker)
            patterns["convergence"] = [
                w for w, workers in word_map.items()
                if len(workers) >= 2
            ][:5]

        return patterns

    # ── PHASE 3: REASON (Chain of Thought) ──────────────────────────────────

    def _reason(self, perception, patterns):
        """Chain-of-thought reasoning over observations and patterns."""
        self.cot.reset()
        score = 0

        # ── Thought 1: Worker Utilization Analysis ──
        idle = [n for n, i in perception["workers"].items() if i["state"] == "IDLE"]
        busy = [n for n, i in perception["workers"].items() if i["state"] in ("PROCESSING", "TYPING")]
        dead = [n for n, i in perception["workers"].items() if not i["alive"]]
        total_workers = len(perception["workers"])
        utilization = len(busy) / max(total_workers, 1)

        if dead:
            self.cot.think(
                f"{len(dead)} worker(s) DEAD: {','.join(dead)}",
                "Dead workers cannot receive tasks. System capacity degraded.",
                f"CRITICAL: Restore {','.join(d.upper() for d in dead)} immediately"
            )
            score += 100

        if idle and perception["pending_todos"] > 0:
            self.cot.think(
                f"{len(idle)} idle worker(s) with {perception['pending_todos']} pending tasks",
                "Idle capacity exists while work is queued. Dispatch bottleneck.",
                f"URGENT: Dispatch to {','.join(w.upper() for w in idle[:2])}"
            )
            score += 80
        elif idle and perception["pending_todos"] == 0:
            self.cot.think(
                f"{len(idle)} idle worker(s), 0 pending tasks",
                "Workers available but no queued work. Consider self-improvement.",
                f"Workers {','.join(w.upper() for w in idle)} ready for new assignments"
            )
            score += 15

        # ── Thought 2: Result Synthesis ──
        new_results = perception["bus_results"]
        if new_results:
            senders = set(m.get("sender", "?").upper() for m in new_results)
            failure_results = [m for m in new_results
                               if any(kw in str(m.get("content", "")).lower()
                                      for kw in ("failed", "error", "timeout", "blocked"))]
            if failure_results:
                self.cot.think(
                    f"{len(failure_results)} failure report(s) from {', '.join(senders)}",
                    "Task failures require reassignment or root cause analysis.",
                    f"CRITICAL: Review failures from {', '.join(m.get('sender','?').upper() for m in failure_results[:2])}"
                )
                score += 70
            else:
                self.cot.think(
                    f"{len(new_results)} new result(s) from {', '.join(senders)}",
                    "Results ready for synthesis. Orchestrator should collect and integrate.",
                    f"Synthesize {len(new_results)} result(s) from {'+'.join(senders)}"
                )
                score += 50

            # Convergence insight
            if patterns["convergence"]:
                self.cot.think(
                    f"Multiple workers converged on topics: {', '.join(patterns['convergence'][:3])}",
                    "Convergence indicates strong signal. Cross-reference findings.",
                    f"High-confidence insights on: {', '.join(patterns['convergence'][:3])}"
                )
                score += 20

        # ── Thought 3: Temporal Pattern Analysis ──
        if patterns["stall_pattern"]:
            self.cot.think(
                "Worker state cycling detected (IDLE->PROCESSING->IDLE) without results",
                "Worker may be receiving tasks but failing silently. Investigate.",
                "ALERT: Potential silent failure loop detected"
            )
            score += 40

        if patterns["dispatch_drought"] and perception["pending_todos"] > 0:
            self.cot.think(
                f"No dispatches in 10 minutes but {perception['pending_todos']} pending tasks",
                "Orchestrator may be sleeping or stuck. Wake-up needed.",
                "URGENT: Orchestrator dispatch pipeline stalled"
            )
            score += 60

        if patterns["failure_rate_10m"] >= 3:
            self.cot.think(
                f"{patterns['failure_rate_10m']} failures in last 10 minutes",
                "Elevated failure rate suggests systemic issue (model drift? broken deps?).",
                f"CRITICAL: Failure rate spike -- {patterns['failure_rate_10m']} in 10min"
            )
            score += 50

        # ── Thought 4: Alert Processing ──
        if perception["bus_alerts"]:
            alert_content = str(perception["bus_alerts"][0].get("content", ""))[:60]
            self.cot.think(
                f"{len(perception['bus_alerts'])} active alert(s): {alert_content}",
                "Alerts require orchestrator attention. Triage and respond.",
                f"{len(perception['bus_alerts'])} alert(s) need attention"
            )
            score += 40

        # ── Thought 5: Strategic Assessment ──
        if perception["skynet_status"]:
            status = perception["skynet_status"]
            uptime_h = status.get("uptime_s", 0) / 3600
            done = status.get("tasks_completed", 0)
            total = status.get("tasks_dispatched", 0)
            rate = done / max(uptime_h, 0.01)

            if rate < 1.0 and uptime_h > 0.5:
                self.cot.think(
                    f"Task completion rate: {rate:.1f}/hr over {uptime_h:.1f}h",
                    "Low throughput relative to uptime. Workers may be underutilized.",
                    f"Throughput concern: {rate:.1f} tasks/hr ({done}/{total})"
                )
                score += 15

        # ── Thought 6: Mission Continuity ──
        stale_missions = [m for m in self.active_missions
                          if time.time() - m.get("started", 0) > 600
                          and m.get("status") == "active"]
        if stale_missions:
            names = [m.get("goal", "?")[:30] for m in stale_missions[:2]]
            self.cot.think(
                f"{len(stale_missions)} mission(s) running >10min: {'; '.join(names)}",
                "Long-running missions may be stalled. Check worker progress.",
                f"Check missions: {'; '.join(names)}"
            )
            score += 25

        # ── Thought 7: Self-Calibration ──
        if len(self.prompt_effectiveness) >= 10:
            recent = self.prompt_effectiveness[-10:]
            effective = sum(1 for _, acted in recent if acted)
            eff_rate = effective / len(recent)
            if eff_rate < 0.3:
                # Cap threshold increase -- never go above 80 to ensure urgent items get through
                self.dynamic_threshold = min(self.dynamic_threshold + 5, 80)
                self.cot.think(
                    f"Prompt effectiveness low: {eff_rate:.0%} of last 10 prompts acted on",
                    "Raising threshold to reduce noise. Fewer, higher-quality prompts.",
                    f"Self-calibrated: threshold raised to {self.dynamic_threshold}"
                )
            elif eff_rate > 0.8 and self.dynamic_threshold > 30:
                self.dynamic_threshold = max(self.dynamic_threshold - 5, 30)

        # Routine tick
        score += 5

        return score

    # ── PHASE 4: PREDICT ────────────────────────────────────────────────────

    def _predict(self, perception, patterns):
        """Predictive intelligence: anticipate near-future states."""
        predictions = []

        # Predict worker completion
        for name, info in perception["workers"].items():
            model = info.get("model")
            if not model:
                continue
            if model.state == "PROCESSING":
                avg = model.avg_processing_time()
                elapsed = time.time() - model.state_since
                if avg > 0:
                    remaining = max(0, avg - elapsed)
                    if remaining < 30:
                        predictions.append(f"{name.upper()} likely finishing in ~{int(remaining)}s")
                elif elapsed > 180:
                    predictions.append(f"{name.upper()} processing for {int(elapsed)}s (may be stuck)")

        # Predict capacity need
        total_pending = perception["pending_todos"] + perception["pending_tasks"]
        idle_count = sum(1 for i in perception["workers"].values() if i["state"] == "IDLE")
        if total_pending > idle_count * 2 and idle_count < 4:
            predictions.append(f"Work backlog building: {total_pending} tasks, {idle_count} idle")

        return predictions

    # ── PHASE 5-6: DECIDE + ACT (Generate Prompt) ──────────────────────────

    def _synthesize_prompt(self, perception, patterns, predictions, score):
        """Generate a compact Skynet Intelligence briefing with status + actionable commands.

        Format: 'Skynet Messenger: [STATUS LINE] | [TOP ACTION WITH COMMAND]'
        Example: 'Skynet Intel: Alpha=IDLE Beta=PROC(45s) | 2 results | 1 alert: STUCK | 3 TODOs | Daemons: OK'
        Only generates if there's something to act on (avoids spam).
        """
        # ── Build compact worker status ──
        worker_parts = []
        for name, info in sorted(perception["workers"].items()):
            state = info["state"]
            model = info.get("model")
            if state == "PROCESSING" and model and model.state_since:
                elapsed = int(time.time() - model.state_since)
                if elapsed > 180:
                    worker_parts.append(f"{name[0].upper()}=STUCK({elapsed}s)")
                else:
                    worker_parts.append(f"{name[0].upper()}=PROC({elapsed}s)")
            elif state == "DEAD":
                worker_parts.append(f"{name[0].upper()}=DEAD!")
            elif state == "IDLE":
                worker_parts.append(f"{name[0].upper()}=IDLE")
            else:
                worker_parts.append(f"{name[0].upper()}={state[:4]}")
        status_workers = " ".join(worker_parts)

        # Count unread items (using delivered tracking for dedup)
        results = perception.get("bus_results", [])
        new_results = self._filter_undelivered(results, "result")
        alerts = perception.get("bus_alerts", [])
        new_alerts = self._filter_undelivered(alerts, "alert")
        pending_todos = perception["pending_todos"]
        pending_tasks = perception["pending_tasks"]
        orch_todos = _get_pending_todo_items("orchestrator", limit=3)

        # Daemon health from heartbeat files + alert messages
        daemon_issues = []
        # Check watchdog status file
        watchdog_status = _load_json(DATA_DIR / "watchdog_status.json")
        if watchdog_status and isinstance(watchdog_status, dict):
            for svc in ("skynet", "god_console", "sse_daemon"):
                svc_status = watchdog_status.get(svc, "unknown")
                if svc_status not in ("ok", "unknown"):
                    daemon_issues.append(f"{svc}={svc_status}")
        # Check monitor health file freshness
        monitor_health_file = DATA_DIR / "monitor_health.json"
        if monitor_health_file.exists():
            try:
                mh_age = time.time() - monitor_health_file.stat().st_mtime
                if mh_age > 120:  # monitor should update every ~10s
                    daemon_issues.append("monitor=stale")
            except Exception:
                pass
        elif (DATA_DIR / "monitor.pid").exists():
            daemon_issues.append("monitor=no_health")
        # Also check alerts for daemon issues
        for a in alerts:
            content = str(a.get("content", "")).lower()
            if "daemon" in content and ("dead" in content or "down" in content or "stale" in content):
                daemon_issues.append("alert")
                break
        daemon_status = ",".join(daemon_issues) if daemon_issues else "OK"

        # Build status segments
        status_parts = [status_workers]
        if new_results:
            senders = list({r.get("sender", "?").title() for r in new_results})
            status_parts.append(f"{len(new_results)} result(s) from {','.join(senders[:3])}")
        if new_alerts:
            alert_preview = str(new_alerts[0].get("content", ""))[:40]
            status_parts.append(f"{len(new_alerts)} alert: {alert_preview}")
        if pending_todos > 0:
            status_parts.append(f"{pending_todos} TODOs")
        if orch_todos:
            status_parts.append(f"OrchTODOs: {len(orch_todos)}")
        status_parts.append(f"Daemons: {daemon_status}")

        status_line = " | ".join(status_parts)

        # ── Build action line (most urgent action with executable command) ──
        actions = []  # (priority, text)

        # Critical/urgent from chain-of-thought
        if self.cot.has_conclusions():
            for c in self.cot.critical_conclusions()[:1]:
                actions.append((100, f"CRITICAL: {c.strip()} Run: python tools/orch_realtime.py status"))

        # Explicit orchestrator TODOs should outrank generic status summaries.
        if orch_todos:
            top_todo = orch_todos[0]
            pri = str(top_todo.get("priority", "normal")).upper()
            task = str(top_todo.get("task", "pending orchestrator task"))[:160]
            actions.append((97, f"EXECUTE ORCH TODO [{pri}]: {task}"))
            if len(orch_todos) > 1:
                next_todo = orch_todos[1]
                next_pri = str(next_todo.get("priority", "normal")).upper()
                next_task = str(next_todo.get("task", "next orchestrator task"))[:120]
                actions.append((96, f"NEXT TODO [{next_pri}]: {next_task}"))

        # Anomalies
        if patterns["stall_pattern"]:
            actions.append((90, "Worker stall detected. Run: python tools/orch_realtime.py status"))
        if patterns["failure_rate_10m"] >= 3:
            actions.append((85, f"Failure spike: {patterns['failure_rate_10m']} in 10min. Run: python tools/orch_realtime.py pending"))
        if patterns["dispatch_drought"] and pending_todos > 0:
            actions.append((80, "Dispatch drought. Run: python tools/orch_realtime.py status -- dispatch TODOs NOW."))

        # New results
        if new_results:
            actions.append((70, "ACTION: Run python tools/orch_realtime.py pending -- read and act on results."))

        # Idle workers + pending TODOs
        idle_workers = [n for n, i in perception["workers"].items() if i["state"] == "IDLE"]
        if idle_workers and pending_todos > 0:
            todo_list = _get_pending_todo_items(limit=10)
            if todo_list:
                top_todo = todo_list[0].get("task", "next task")[:50]
                worker = idle_workers[0]
                actions.append((60, f"DISPATCH: python tools/skynet_dispatch.py --worker {worker} --task \"{top_todo}\""))
        elif idle_workers and not pending_todos and not new_results:
            # Planner-driven mission generation
            mission_goal, subtasks = self._plan_autonomous_mission(perception, patterns)
            if mission_goal and subtasks:
                subtask_preview = subtasks[0][:50] if subtasks else ""
                target_worker = idle_workers[0]
                actions.append((55, f"MISSION: \"{mission_goal[:60]}\" -- python tools/skynet_dispatch.py --worker {target_worker} --task \"{subtask_preview}\""))

        # Alerts with action
        if new_alerts:
            alert_content = str(new_alerts[0].get("content", ""))[:80]
            actions.append((75, f"ALERT: {alert_content}"))

        # ── Decide if this is worth sending ──
        # Only send if there is SOMETHING to act on
        has_actionable = (
            len(new_results) > 0
            or len(new_alerts) > 0
            or (idle_workers and pending_todos > 0)
            or patterns["stall_pattern"]
            or patterns["failure_rate_10m"] >= 3
            or any(not i["alive"] for i in perception["workers"].values())
            or self.cot.has_conclusions()
            or daemon_status != "OK"
        )

        if not has_actionable:
            return ""  # Empty string = don't send (no spam)

        # Mark delivered
        self._mark_delivered(new_results, "result")
        self._mark_delivered(new_alerts, "alert")

        # Build final prompt
        actions.sort(key=lambda x: x[0], reverse=True)
        top_action = actions[0][1] if actions else "System needs attention."

        prompt_counter = f"({self.consecutive_prompts + 1}/{self.max_consecutive})"
        prompt = f"Skynet Intel: {status_line} || {top_action} {prompt_counter}"

        return prompt

    def _filter_undelivered(self, messages, category):
        """Filter out messages that have already been delivered to the orchestrator."""
        delivered = self._load_delivered()
        delivered_ids = set(delivered.get(category, []))
        return [m for m in messages if m.get("id", "") not in delivered_ids]

    def _mark_delivered(self, messages, category):
        """Mark message IDs as delivered so they won't be sent again."""
        if not messages:
            return
        delivered = self._load_delivered()
        ids = delivered.setdefault(category, [])
        for m in messages:
            mid = m.get("id", "")
            if mid and mid not in ids:
                ids.append(mid)
        # Cap to prevent unbounded growth
        if len(ids) > 500:
            delivered[category] = ids[-300:]
        delivered["updated_at"] = datetime.now().isoformat()
        _save_json(DELIVERED_FILE, delivered)

    def _load_delivered(self):
        """Load the delivered message tracking file."""
        data = _load_json(DELIVERED_FILE)
        if data and isinstance(data, dict):
            return data
        return {"result": [], "alert": [], "updated_at": ""}

    def _gather_intelligence(self):
        """LEGACY WRAPPER -- routes to the new chain-of-thought pipeline.
        Returns list of section strings for backward compatibility."""
        perception = self._perceive()
        patterns = self._remember(perception)
        score = self._reason(perception, patterns)
        predictions = self._predict(perception, patterns)
        prompt = self._synthesize_prompt(perception, patterns, predictions, score)
        # Return as single-element list (legacy format)
        return [prompt]

    def _boot_prompt(self):
        """Fire ONCE on daemon start -- gather system state and send actionable brief."""
        if self._boot_done:
            return
        self._boot_done = True
        log("Boot prompt: waiting for orchestrator IDLE...")

        orch_hwnd = _get_orch_hwnd()
        if not orch_hwnd or not _is_window_alive(orch_hwnd):
            log("Boot prompt: orchestrator window not found", "WARN")
            return

        # Wait up to 60s for orchestrator to be IDLE
        for _ in range(30):
            state = _get_worker_state(orch_hwnd)
            if state == "IDLE":
                break
            time.sleep(2)
        else:
            log("Boot prompt: orchestrator not IDLE after 60s -- sending anyway", "WARN")

        sections = self._gather_intelligence()
        if not sections:
            sections = ["[STATUS] System clean, no pending work."]

        prompt = "Skynet Messenger: BOOT BRIEF -- " + " | ".join(sections)

        log(f"Boot prompt: {prompt[:200]}...")
        ok = _send_self_prompt(orch_hwnd, prompt)
        if ok:
            log("Boot prompt DELIVERED", "OK")
            self.last_prompt_time = time.time()
            _append_log({
                "timestamp": datetime.now().isoformat(),
                "trigger_reasons": ["boot"],
                "prompt": prompt[:300],
                "delivered": True,
            })
        else:
            log("Boot prompt FAILED", "ERROR")

    def _is_dispatch_active(self):
        """Check if dispatch is currently typing into a worker (collision guard)."""
        try:
            if DISPATCH_LOCK_FILE.exists():
                lock_age = time.time() - DISPATCH_LOCK_FILE.stat().st_mtime
                return lock_age < 30  # lock valid for 30s
        except Exception:
            pass
        return False

    def _is_boot_in_progress(self):
        """Check if skynet_start.py boot is running (UIA-heavy phases 3-6).
        
        During boot, multiple UIA operations hit VS Code in rapid succession
        (guard_model, prompt_worker, session restore). Sending a self-prompt
        during this window causes clipboard contention and VS Code sticking.
        """
        try:
            if BOOT_IN_PROGRESS_FILE.exists():
                boot_age = time.time() - BOOT_IN_PROGRESS_FILE.stat().st_mtime
                if boot_age < 300:  # boot lock valid for up to 5 minutes
                    return True
                # Stale lock — boot probably crashed; clean up
                BOOT_IN_PROGRESS_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return False

    def _write_health_file(self):
        """Write daemon health to persistent file EVERY cycle.

        This persists independently of the bus ring buffer so the orchestrator
        can always check daemon health via data/self_prompt_health.json.
        """
        now = time.time()
        orch_hwnd = _get_orch_hwnd()
        health = {
            "daemon": "self_prompt",
            "cycles": self.cycles,
            "sent": self.prompts_sent,
            "failed": self.prompts_failed,
            "last_sent_timestamp": getattr(self, 'last_prompt_time', 0.0),
            "orchestrator_hwnd": orch_hwnd,
            "pid": os.getpid(),
            "uptime_s": int(now - getattr(self, '_start_time', now)),
            "timestamp": datetime.now().isoformat(),
        }
        try:
            _save_json(HEALTH_FILE, health)
        except Exception:
            pass  # never crash the daemon for a health write

    def _report_health(self):
        """Post health status to bus periodically (separate from per-cycle file write)."""
        now = time.time()
        if now - self.last_health_report < HEALTH_REPORT_INTERVAL:
            return
        self.last_health_report = now
        _post_bus("orchestrator", "daemon_health",
                  f"SELF_PROMPT_HEALTH: cycles={self.cycles} sent={self.prompts_sent} failed={self.prompts_failed}")
        log(f"Health report posted (cycles={self.cycles}, sent={self.prompts_sent})")

    def _scan_triggers(self):
        """CHAIN-OF-THOUGHT trigger system.

        Runs the full 7-phase intelligence pipeline:
          PERCEIVE -> REMEMBER -> REASON -> PREDICT -> DECIDE -> SYNTHESIZE -> LEARN

        Returns (should_prompt, reasons, prompt_text, score).
        """
        reasons = []

        orch_hwnd = _get_orch_hwnd()
        if not orch_hwnd or not _is_window_alive(orch_hwnd):
            return False, ["orch_window_dead"], "", 0

        # Collision guard: don't prompt while dispatch is typing into a worker
        if self._is_dispatch_active():
            return False, ["dispatch_active"], "", 0

        # Boot guard: don't prompt while skynet_start.py is running UIA-heavy phases
        if self._is_boot_in_progress():
            return False, ["boot_in_progress"], "", 0

        # Only block for TYPING (user actively typing). PROCESSING is fine.
        orch_state = _get_worker_state(orch_hwnd)
        if orch_state == "TYPING":
            return False, ["orch_typing"], "", 0

        # ── Full Chain-of-Thought Pipeline ──
        perception = self._perceive()           # Phase 1: scan everything
        patterns = self._remember(perception)   # Phase 2: temporal patterns
        score = self._reason(perception, patterns)  # Phase 3: chain-of-thought
        predictions = self._predict(perception, patterns)  # Phase 4: anticipate

        # Extract reasons from chain-of-thought steps
        for step in self.cot.steps:
            conclusion = step.get("conclusion", "")
            if "CRITICAL" in conclusion.upper():
                reasons.append("critical_finding")
            elif "URGENT" in conclusion.upper():
                reasons.append("urgent_finding")
            elif "Dispatch" in conclusion or "dispatch" in conclusion:
                reasons.append("dispatch_opportunity")
            elif "Synthesize" in conclusion:
                reasons.append("results_ready")
            elif "failure" in conclusion.lower() or "FAILURE" in conclusion:
                reasons.append("failure_detected")
            elif "anomaly" in conclusion.lower() or "ANOMALY" in conclusion.upper():
                reasons.append("anomaly_detected")

        if not reasons:
            reasons.append("routine_tick")

        # Accumulate with carry-forward (uses self-calibrating threshold)
        total_score = self.accumulated_score + score
        should_prompt = total_score >= self.dynamic_threshold

        # URGENT BYPASS: alerts, pending results, and dispatch drought ALWAYS force a prompt
        # regardless of threshold. This prevents the daemon from going silent when action is needed.
        urgent_bypass = False
        if "critical_finding" in reasons or "urgent_finding" in reasons:
            urgent_bypass = True
        elif "results_ready" in reasons and total_score >= 30:
            urgent_bypass = True
        elif "failure_detected" in reasons:
            urgent_bypass = True

        prompt_text = ""
        if should_prompt or urgent_bypass:
            prompt_text = self._synthesize_prompt(perception, patterns, predictions, total_score)
            if not prompt_text:
                # Synthesizer determined nothing actionable -- don't spam
                self.accumulated_score = total_score  # keep score for next cycle
                return False, ["nothing_actionable"], "", total_score
            self.accumulated_score = 0
            bypass_tag = " [URGENT_BYPASS]" if (urgent_bypass and not should_prompt) else ""
            self._record_event(EVT_PROMPT, "system", f"score={total_score} reasons={','.join(reasons[:3])}{bypass_tag}")

            # Phase 7: LEARN -- save persistent state every prompt
            self._save_persistent_state()
        else:
            self.accumulated_score = total_score

        return (should_prompt or urgent_bypass), reasons, prompt_text, total_score

    def _deliver_queued_directives(self):
        """Check orch_queue.json for pending directives and type them into the orchestrator."""
        try:
            if not ORCH_QUEUE_FILE.exists():
                return 0
            data = json.loads(ORCH_QUEUE_FILE.read_text(encoding="utf-8"))
            queue = data.get("queue", [])
            pending = [e for e in queue if e.get("status") == "pending"]
            if not pending:
                return 0

            orch_hwnd = _get_orch_hwnd()
            if not orch_hwnd or not _is_window_alive(orch_hwnd):
                return 0

            # Only block if user is actively typing
            state = _get_worker_state(orch_hwnd)
            if state == "TYPING":
                return 0

            delivered = 0
            for entry in pending:
                content = entry.get("content", "")
                msg_id = entry.get("msg_id", "")
                sender = entry.get("sender", "?")
                priority = str(entry.get("priority", "normal")).upper()
                if not content:
                    continue

                prompt = (
                    f"SKYNET {priority} DIRECTIVE from {sender}. "
                    f"Act now. Use workers for implementation, not hands-on orchestrator edits. "
                    f"Directive: {content}"
                )
                log(f"Delivering queued directive from {sender}: {content[:60]}...")

                ok = _send_self_prompt(orch_hwnd, prompt)
                if ok:
                    entry["status"] = "delivered"
                    entry["delivered_at"] = datetime.now().isoformat()
                    data["stats"]["total_delivered"] = data["stats"].get("total_delivered", 0) + 1
                    delivered += 1
                    log(f"Queued directive DELIVERED (msg_id={msg_id})")
                    self.last_prompt_time = time.time()  # update rate limit
                    time.sleep(2)  # gap between directives
                else:
                    log(f"Queued directive delivery FAILED", "ERROR")
                    break  # stop trying if delivery fails

            if delivered > 0:
                ORCH_QUEUE_FILE.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

            return delivered
        except Exception as e:
            log(f"Queued directive delivery error: {e}", "ERROR")
            return 0

    def check_and_prompt(self, deliver_queue_first=True):
        """Single check cycle with priority scoring and anti-spam. Returns True if prompt was sent."""
        self.cycles += 1
        now = time.time()

        # Health self-report
        self._report_health()

        # First: deliver any queued directives (these take priority)
        if deliver_queue_first:
            queued_delivered = self._deliver_queued_directives()
            if queued_delivered > 0:
                log(f"Delivered {queued_delivered} queued directive(s)")
                return True

        # Rate limit — use shorter gap when all workers idle
        effective_gap = MIN_PROMPT_GAP
        if self._all_workers_idle():
            effective_gap = min(ALL_IDLE_INTERVAL, MIN_PROMPT_GAP)
        if now - self.last_prompt_time < effective_gap:
            return False

        # Consecutive prompt limiter: reset if orchestrator acted independently
        if self._orchestrator_took_action():
            if self.consecutive_prompts > 0:
                log(f"Orchestrator took action -- resetting consecutive counter ({self.consecutive_prompts} -> 0)")
            self.consecutive_prompts = 0

        if self.consecutive_prompts >= self.max_consecutive:
            log(f"Consecutive limit reached ({self.consecutive_prompts}/{self.max_consecutive}) -- waiting for orchestrator action")
            return False

        should, reasons, prompt, total_score = self._scan_triggers()

        if not should:
            return False  # score below threshold

        orch_hwnd = _get_orch_hwnd()
        if not orch_hwnd:
            log("No orchestrator HWND", "ERROR")
            return False

        # COLLISION GUARD: Only block if user is actively TYPING.
        # PROCESSING is fine -- VS Code queues input after current generation.
        # This is the GOD Protocol: prompts queue naturally like user messages.
        try:
            sys.path.insert(0, str(ROOT / "tools"))
            from uia_engine import get_engine
            engine = get_engine()
            orch_state = engine.get_state(orch_hwnd)
            if orch_state == "TYPING":
                log(f"Orchestrator TYPING -- skipping prompt (collision guard)")
                return False
            if orch_state == "PROCESSING":
                log(f"Orchestrator PROCESSING -- queuing prompt (VS Code will queue)")
        except Exception as e:
            log(f"State check failed: {e} -- prompting anyway", "WARN")

        # Anti-spam: dedup identical prompts within 60s
        prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:12]
        if prompt_hash == self.last_prompt_hash and (now - self.last_prompt_hash_time) < 60:
            log(f"Duplicate prompt suppressed (hash={prompt_hash})")
            return False

        log(f"TRIGGERING (score={total_score}): {', '.join(reasons)}")
        log(f"Prompt: {prompt[:120]}...")

        ok = _send_self_prompt(orch_hwnd, prompt)

        self.last_prompt_time = now
        self.last_prompt_hash = prompt_hash
        self.last_prompt_hash_time = now
        entry = {
            "timestamp": datetime.now().isoformat(),
            "trigger_reasons": reasons,
            "score": total_score,
            "prompt": prompt[:200],
            "delivered": ok,
        }
        _append_log(entry)
        _update_last_action("self_prompt_sent")

        if ok:
            self.prompts_sent += 1
            self.consecutive_prompts += 1
            log(f"Prompt DELIVERED (consecutive {self.consecutive_prompts}/{self.max_consecutive})")
            _post_bus("orchestrator", "self_prompt",
                      f"Self-prompt sent (score={total_score}, {self.consecutive_prompts}/{self.max_consecutive}): {', '.join(reasons)}")
        else:
            self.prompts_failed += 1
            log("Prompt FAILED to deliver", "ERROR")

        return ok

    def _all_workers_idle(self):
        """Check if ALL 4 workers (alpha/beta/gamma/delta) report IDLE via /status endpoint.
        Returns True only when every registered worker is IDLE. If any is busy, returns False.
        Uses a 30-second cache to avoid expensive HTTP calls every 5s cycle.
        """
        now = time.time()
        cache_ttl = 30.0
        if (hasattr(self, '_worker_state_cache') and
                hasattr(self, '_worker_state_cache_ts') and
                now - self._worker_state_cache_ts < cache_ttl):
            return self._worker_state_cache

        result = self._fetch_all_workers_idle()
        self._worker_state_cache = result
        self._worker_state_cache_ts = now
        return result

    def _fetch_all_workers_idle(self):
        """Actual HTTP call to check worker idle status."""
        try:
            data = _fetch_json(f"{BUS_URL}/status")
            if not data:
                return False
            agents = data.get("agents", {})
            if not agents:
                return False
            worker_names = {"alpha", "beta", "gamma", "delta"}
            for name in worker_names:
                agent = agents.get(name, {})
                status = agent.get("status", "unknown").upper()
                if status != "IDLE":
                    return False
            return True
        except Exception:
            return False

    def run(self):
        """Main daemon loop -- STATUS-BASED, not timer-based.

        Polls /status every 5 seconds. Only triggers check_and_prompt() when
        ALL 4 workers report IDLE. If any worker is busy, the cycle is skipped.
        This prevents the daemon from prompting the orchestrator mid-wave.
        """
        self._start_time = time.time()
        log("Self-prompt daemon starting (status-based, 5s poll)")
        log(f"Config: poll=5s gap={MIN_PROMPT_GAP}s threshold={PROMPT_THRESHOLD} idle_thresh={IDLE_WORKER_THRESHOLD}s")
        _post_bus("orchestrator", "monitor_alert",
                  "SELF_PROMPT_ONLINE: Orchestrator heartbeat daemon started (status-based polling)")

        # Boot prompt: wait for skynet-start boot phases to complete before touching orchestrator
        # Phase 8 launches us, but phases 3-6 may still be finishing UIA operations
        boot_wait = 0
        while self._is_boot_in_progress() and boot_wait < 120:
            time.sleep(5)
            boot_wait += 5
            if boot_wait % 15 == 0:
                log(f"Waiting for boot to complete ({boot_wait}s)...")
        if boot_wait > 0:
            log(f"Boot completed after {boot_wait}s wait", "OK")
        else:
            time.sleep(5)  # minimal settle time even if no boot lock
        try:
            self._boot_prompt()
        except Exception as e:
            log(f"Boot prompt failed: {e}", "ERROR")

        POLL_INTERVAL = 5  # check status every 5 seconds

        try:
            while True:
                try:
                    # Hot-reload config every cycle (file read is cheap, ~0.1ms)
                    _load_config_overrides()

                    # Persist health to file EVERY cycle (survives bus ring buffer rotation)
                    self._write_health_file()

                    # Always deliver queued directives, even mid-wave.
                    queued_delivered = self._deliver_queued_directives()
                    if queued_delivered > 0:
                        log(f"Delivered {queued_delivered} queued directive(s)")
                    # STATUS-BASED: only synthesize generic prompts when ALL workers are IDLE
                    elif self._all_workers_idle():
                        self.check_and_prompt(deliver_queue_first=False)
                except Exception as e:
                    log(f"Check failed: {e}", "ERROR")
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log("Shutting down (Ctrl+C)")
        finally:
            _post_bus("orchestrator", "monitor_alert",
                      "SELF_PROMPT_OFFLINE: Heartbeat daemon stopped")
            if PID_FILE.exists():
                try:
                    PID_FILE.unlink()
                except Exception:
                    pass


def _check_existing():
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            out = _hidden_check_output(
                ["tasklist", "/fi", f"pid eq {old_pid}", "/fo", "csv", "/nh"],
                text=True, timeout=5, stderr=subprocess.DEVNULL
            )
            if str(old_pid) in out and "python" in out.lower():
                return old_pid
        except Exception:
            pass
    return None


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skynet Self-Prompt -- Orchestrator Heartbeat")
    parser.add_argument("action", nargs="?", default="status",
                        choices=["start", "status", "once", "stop"],
                        help="start=daemon, status=last prompt, once=single check, stop=show PID")
    args = parser.parse_args()

    if args.action == "status":
        log_data = _load_json(LOG_FILE)
        if log_data and isinstance(log_data, list) and len(log_data) > 0:
            last = log_data[-1]
            print(json.dumps(last, indent=2))
            print(f"\nTotal prompts logged: {len(log_data)}")
        else:
            print("No self-prompt log found.")
        pid = _check_existing()
        if pid:
            print(f"Daemon running: PID {pid}")
        else:
            print("Daemon not running.")
        return

    if args.action == "once":
        daemon = SelfPromptDaemon()
        daemon.check_and_prompt()
        return

    if args.action == "stop":
        pid = _check_existing()
        if pid:
            print(f"Self-prompt daemon running as PID {pid}.")
        else:
            print("Daemon not running.")
        return

    if args.action == "start":
        existing = _check_existing()
        if existing:
            print(f"Already running (PID {existing}). Use 'status' to check.")
            return

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))
        log(f"Self-prompt daemon PID {os.getpid()}")

        # Seed last action to now to avoid immediate prompting
        _update_last_action("daemon_start")

        daemon = SelfPromptDaemon()
        daemon.run()


if __name__ == "__main__":
    main()
