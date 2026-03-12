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
import re
import signal
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
DAEMON_NAME = "self_prompt"
DAEMON_VERSION = "1.2.0"

# Defaults (overridden by brain_config.json -> self_prompt section)
LOOP_INTERVAL = 300
MIN_PROMPT_GAP = 45
PROMPT_THRESHOLD = 50
IDLE_WORKER_THRESHOLD = 90    # raised: 30s was too aggressive, workers need time
ORCH_INACTIVE_THRESHOLD = 45
MAX_LOG_ENTRIES = 50
HEALTH_REPORT_INTERVAL = 300  # report health to bus every 5 min
MAX_CONSECUTIVE_PROMPTS = 2   # stop after N prompts without orchestrator action
REPEATED_STATE_SUPPRESSION_S = 600  # suppress repeated no-change prompts for 10 minutes  # signed: consultant

ALL_IDLE_INTERVAL = 60  # faster prompting when all workers idle
BOOT_PROMPT_ENABLED = True

# Self-prompt generation is template-only. Ollama is removed from the live pipeline.  # signed: consultant
REQUIRED_WORKERS = ("alpha", "beta", "gamma", "delta")

def _load_config_overrides():
    """Load self_prompt thresholds from brain_config.json. Called on startup AND each cycle (hot-reload)."""
    global LOOP_INTERVAL, MIN_PROMPT_GAP, IDLE_WORKER_THRESHOLD
    global ORCH_INACTIVE_THRESHOLD, HEALTH_REPORT_INTERVAL, PROMPT_THRESHOLD, MAX_CONSECUTIVE_PROMPTS
    global ALL_IDLE_INTERVAL, BOOT_PROMPT_ENABLED
    try:
        cfg = json.loads(BRAIN_CONFIG_FILE.read_text(encoding="utf-8"))
        sp = cfg.get("self_prompt", {})
        god = cfg.get("god_protocol", {})
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
        if "boot_prompt_enabled" in god:
            BOOT_PROMPT_ENABLED = bool(god["boot_prompt_enabled"])
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
    msg = {"sender": "self_prompt", "topic": topic, "type": msg_type, "content": content}
    try:
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish(msg)
        return result.get("allowed", False)
    except ImportError:
        # Fallback: direct HTTP if SpamGuard not available
        try:
            payload = json.dumps(msg).encode()
            req = urllib.request.Request(
                f"{BUS_URL}/bus/publish", payload,
                {"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=5)
            return True
        except Exception:
            return False
    except Exception:
        return False
    # signed: gamma


def _get_skynet_version():
    """Return Skynet version string, or None if unavailable (Truth: never fabricate)."""
    try:
        from tools.skynet_version import current_version
        cur = current_version() or {}
        version = cur.get("version")
        return str(version) if version else None
    except Exception:
        return None
    # signed: delta


def _versioned_signal(prefix, text):
    return f"{prefix} v{DAEMON_VERSION}: {text}"


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
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        from skynet_delivery import resolve_orchestrator_hwnd
        return int(resolve_orchestrator_hwnd() or 0)
    except Exception:
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


def _verify_orch_delivery(orch_hwnd, pre_state, timeout_s=8):
    """Verify self-prompt delivery by checking orchestrator state transition.

    Polls UIA for up to timeout_s seconds to confirm orchestrator moved from
    pre_state (usually IDLE) to PROCESSING. Returns True if state changed.
    """
    if pre_state == "PROCESSING":
        return True  # already processing, prompt queued in VS Code
    try:
        from uia_engine import get_engine
        engine = get_engine()
        for _ in range(timeout_s * 2):  # poll every 0.5s
            time.sleep(0.5)
            try:
                post_state = engine.get_state(orch_hwnd)
                if post_state != pre_state:
                    log(f"Delivery VERIFIED: orchestrator {pre_state} -> {post_state}", "OK")
                    return True
            except Exception:
                pass
        log(f"Delivery UNVERIFIED: orchestrator stayed {pre_state} after {timeout_s}s", "WARN")
        return False
    except Exception as e:
        log(f"Delivery verify error: {e}", "WARN")
        return True  # don't block on verify infra failure
    # signed: gamma


def _send_self_prompt(orch_hwnd, prompt_text):
    """Type a prompt into the orchestrator's chat window with delivery verification.

    After delivering, verifies orchestrator state changed (IDLE -> PROCESSING).
    Retries once after 3s if first delivery fails verification.
    Posts SELF_PROMPT_DELIVERY_FAILED alert if both attempts fail.
    """
    sys.path.insert(0, str(ROOT / "tools"))

    for attempt in range(1, 3):
        try:
            pre_state = _get_worker_state(orch_hwnd) or "UNKNOWN"
            t0 = time.time()

            from skynet_delivery import deliver_to_orchestrator
            result = deliver_to_orchestrator(prompt_text, sender="self_prompt", also_bus=False)
            ok = bool(result.get("success"))
            elapsed_ms = int((time.time() - t0) * 1000)

            if not ok:
                log(f"Attempt {attempt}: delivery call returned failure ({elapsed_ms}ms)", "WARN")
                if attempt == 1:
                    time.sleep(3)
                continue

            verified = _verify_orch_delivery(orch_hwnd, pre_state, timeout_s=8)
            if verified:
                log(f"Attempt {attempt}: delivery OK + verified ({elapsed_ms}ms)", "OK")
                return True

            log(f"Attempt {attempt}: delivery OK but state unchanged ({elapsed_ms}ms, pre={pre_state})", "WARN")
            if attempt == 1:
                time.sleep(3)

        except Exception as e:
            log(f"Attempt {attempt}: send exception: {e}", "ERROR")
            if attempt == 1:
                time.sleep(3)

    # Both attempts failed — post alert to bus
    log("SELF_PROMPT_DELIVERY_FAILED after 2 attempts", "ERROR")
    _post_bus("orchestrator", "alert",
              _versioned_signal("SELF_PROMPT_DELIVERY_FAILED",
                                "Self-prompt delivery failed after 2 attempts (delivery unverified)"))
    return False
    # signed: gamma


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
            return None  # Truth: unknown, not fabricated 0.5 # signed: delta
        return self.tasks_completed / total

    def to_dict(self):
        eff = self.effectiveness_score()
        return {
            "name": self.name, "state": self.state,
            "idle_s": round(self.idle_duration()),
            "tasks": self.tasks_completed, "fails": self.tasks_failed,
            "avg_proc_s": round(self.avg_processing_time()),
            "load": round(self.cognitive_load, 2),
            "effectiveness": round(eff, 2) if eff is not None else None,
            "stalls": self.stall_count,
        }
        # signed: delta


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
        """Return only genuinely critical conclusions (system failures, not idle workers)."""
        dominated = []
        for s in self.steps:
            c = s.get("conclusion", "").upper()
            # Only surface CRITICAL for real system failures (DEAD, DOWN, security)
            # URGENT for genuine stalls (dispatch drought, failure spikes) -- NOT idle workers
            if "CRITICAL" in c and any(kw in c for kw in ("DEAD", "DOWN", "SECURITY", "RESTORE", "FAILURE RATE")):
                dominated.append(s["conclusion"])
            # URGENT only for genuine system failures -- NOT dispatch pauses
            elif "URGENT" in c and any(kw in c for kw in ("SPIKE", "SECURITY", "DEAD")):
                dominated.append(s["conclusion"])
        return dominated

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
        self.last_prompt_state_hash = ""
        self.last_prompt_state_hash_time = 0.0
        self.pending_prompt_state_hash = ""
        self.prompts_sent = 0
        self.prompts_failed = 0
        self.cycles = 0
        self._boot_done = False

        # Restore delivered IDs from persistent file
        delivered = _load_json(DELIVERED_FILE)
        if delivered and isinstance(delivered, dict):
            self.seen_result_ids = set(delivered.get("result", []))
            self.seen_alert_ids = set(delivered.get("alert", []))

        # Consecutive prompt limiter
        self.consecutive_prompts = 0
        self.max_consecutive = MAX_CONSECUTIVE_PROMPTS
        self.last_dispatch_check_count = 0
        self.last_cycle_complete_time = 0.0
        self.all_idle_since = 0.0

        self._init_intelligence()
        self._load_persistent_state()

    def _init_intelligence(self):
        """Initialize intelligence subsystems (temporal, cognitive, mission, calibration)."""
        self.event_history = []
        self.EVENT_WINDOW = 500
        self.worker_models = {}
        self.active_missions = []
        self.mission_counter = 0
        self.prompt_effectiveness = []
        self.dynamic_threshold = PROMPT_THRESHOLD
        self.dynamic_gap = MIN_PROMPT_GAP
        self.cot = ChainOfThought()
        self.baseline_dispatch_rate = 0.0
        self.baseline_completion_rate = 0.0
        self.anomaly_alerts = []

        # Planner integration
        self._planner = None
        self._last_mission_plan_time = 0.0
        self._mission_plan_cooldown = 300
        self._mission_plan_history = []
        self._init_planner()

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
        """Generate an autonomous improvement mission when workers are idle and no work exists.
        
        Returns: (mission_goal, subtask_descriptions) or (None, None).
        """
        now = time.time()
        if now - self._last_mission_plan_time < self._mission_plan_cooldown:
            return None, None

        idle_workers = [n for n, i in perception["workers"].items() 
                       if i.get("state") == "IDLE" and i.get("alive")]
        if not idle_workers:
            return None, None

        pending = perception.get("pending_todos", 0) + perception.get("pending_tasks", 0)
        if pending > 0:
            return None, None

        goal = self._identify_improvement_goal(perception, patterns)
        if not goal:
            return None, None

        if self._planner:
            try:
                plan = self._planner.create_plan(goal, context=self._build_planning_context(perception))
                subtasks = [st.description for st in plan.subtasks]
                self._register_mission(goal, subtasks, idle_workers, now)
                return goal, subtasks
            except Exception as e:
                log(f"Planner decomposition failed: {e}", "WARN")

        self._last_mission_plan_time = now
        return goal, [goal]

    def _register_mission(self, goal, subtasks, idle_workers, now):
        """Record a planned mission in active missions and history."""
        self._last_mission_plan_time = now
        self.mission_counter += 1
        self.active_missions.append({
            "id": f"mission_{self.mission_counter}",
            "goal": goal,
            "subtasks": subtasks,
            "dispatched_to": idle_workers[:len(subtasks)],
            "started": now,
            "status": "planned",
            "source": "planner",
        })
        self._mission_plan_history.append({
            "goal": goal,
            "subtasks": len(subtasks),
            "time": datetime.now().isoformat(),
        })
        if len(self._mission_plan_history) > 20:
            self._mission_plan_history = self._mission_plan_history[-20:]
        log(f"PLANNER: Generated mission '{goal}' with {len(subtasks)} subtasks")

    _COGNITIVE_GOALS = [
        "Wire core/cognitive/reflexion.py into the dispatch pipeline -- after task failures, auto-generate verbal self-critiques and store in learning_store",
        "Wire core/cognitive/graph_of_thoughts.py into skynet_brain.py -- use GoT for complex multi-branch reasoning on COMPLEX/ADVERSARIAL tasks",
        "Wire core/cognitive/knowledge_distill.py as a background daemon -- consolidate decaying episodic memories into durable semantic knowledge",
        "Wire core/cognitive/mcts.py into web navigation tasks -- use R-MCTS for autonomous browser interaction planning",
    ]

    _GENERAL_GOALS = [
        "Audit and improve test coverage for tools/skynet_*.py -- identify untested functions and add targeted tests",
        "Review and optimize the dispatch pipeline -- measure latency, identify bottlenecks, reduce overhead",
        "Update AGENTS.md documentation to reflect Level 4 capabilities and new features",
        "Scan codebase for TODO/FIXME/HACK comments and create actionable improvement tickets",
        "Profile and optimize the UIA engine scan performance -- target sub-100ms per scan",
    ]

    def _identify_improvement_goal(self, perception, patterns) -> str:
        """Analyze system state to identify the highest-impact improvement goal.
        
        Priority: failures > dormant engines > low IQ > cognitive wiring > general.
        """
        if patterns.get("failure_rate_10m", 0) >= 2:
            return "Investigate and fix recent task failures -- check worker logs, bus error messages, and dispatch pipeline"

        dormant = self._find_dormant_engine(perception)
        if dormant:
            return f"Activate dormant engine '{dormant}' -- investigate why it is 'available' but not 'online', fix dependencies"

        iq_data = perception.get("iq")
        if iq_data and isinstance(iq_data, dict):
            iq_val = iq_data.get("iq", 0)
            if iq_val < 0.80:
                return f"Improve collective IQ (currently {iq_val:.4f}) -- audit low-scoring engines, add missing capabilities, improve test coverage"

        return self._pick_novel_goal(self._COGNITIVE_GOALS) or self._pick_novel_goal(self._GENERAL_GOALS) or ""

    def _find_dormant_engine(self, perception) -> str:
        """Return the name of the first engine that is 'available' but not 'online', or ''."""
        engine_status = perception.get("engine_status")
        if not engine_status or not isinstance(engine_status, dict):
            return ""
        engines = engine_status.get("engines", {})
        for name, info in engines.items():
            if isinstance(info, dict) and info.get("status") == "available":
                return name
        return ""

    def _pick_novel_goal(self, goal_list) -> str:
        """Return the first goal from goal_list not recently planned, or ''."""
        recent = {m.get("goal", "") for m in self._mission_plan_history[-10:]}
        for g in goal_list:
            if g not in recent:
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
        self._perceive_workers(perception)
        self._perceive_bus(perception)
        perception["pending_todos"] = _get_pending_todos()
        perception["pending_tasks"] = _get_pending_tasks()
        self._perceive_metadata(perception)
        return perception

    def _perceive_workers(self, perception):
        """Scan worker states via UIA and update cognitive models."""
        workers = _load_workers()
        for w in workers:
            name = w.get("name", "?")
            hwnd = w.get("hwnd", 0)
            alive = hwnd and _is_window_alive(hwnd)
            state = "DEAD"
            if alive:
                state = _get_worker_state(hwnd)

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

    def _perceive_bus(self, perception):
        """Process bus messages into results and alerts."""
        bus_msgs = _fetch_json(f"{BUS_URL}/bus/messages?limit=50")
        if not bus_msgs or not isinstance(bus_msgs, list):
            return

        for m in bus_msgs:
            mid = m.get("id", "")
            msg_type = m.get("type", "")
            msg_topic = m.get("topic", "")

            if msg_type == "result" and msg_topic == "orchestrator":
                if mid and mid not in self.seen_result_ids:
                    self.seen_result_ids.add(mid)
                    perception["bus_results"].append(m)
                    self._record_event(EVT_RESULT, m.get("sender", "?"), str(m.get("content", ""))[:100])
                    self._update_worker_model_from_result(m)

            if msg_type in ("alert", "monitor_alert", "service_alert", "urgent"):
                sender = str(m.get("sender", ""))
                content = str(m.get("content", ""))
                if sender == "self_prompt" and content.startswith("SELF_PROMPT_"):
                    continue
                if mid and mid not in self.seen_alert_ids:
                    self.seen_alert_ids.add(mid)
                    perception["bus_alerts"].append(m)
                    self._record_event(EVT_ALERT, m.get("sender", "?"), str(m.get("content", ""))[:80])

    def _update_worker_model_from_result(self, msg):
        """Update a worker's cognitive model based on a result message."""
        sender = msg.get("sender", "")
        if sender not in self.worker_models:
            return
        content_lower = str(msg.get("content", "")).lower()
        model = self.worker_models[sender]
        if any(kw in content_lower for kw in ("error", "failed", "timeout")):
            model.tasks_failed += 1
            model.last_result_quality = "failure"
            self._record_event(EVT_FAILURE, sender, content_lower[:80])
        else:
            model.tasks_completed += 1
            model.last_result_quality = "success"

    def _perceive_metadata(self, perception):
        """Gather agent profiles, engine status, IQ, and learning store."""
        try:
            pdata = _load_json(DATA_DIR / "agent_profiles.json") or {}
            for k, v in pdata.items():
                if isinstance(v, dict) and k not in ("version", "updated_at", "updated_by"):
                    perception["profiles"][k] = v
        except Exception:
            pass

        perception["skynet_status"] = _fetch_json(f"{BUS_URL}/status")
        perception["engine_status"] = _fetch_json("http://localhost:8421/engines")

        try:
            iq_data = _load_json(DATA_DIR / "iq_history.json") or {}
            h = iq_data.get("history", [])
            if h:
                perception["iq"] = h[-1]
        except Exception:
            pass

        try:
            from core.learning_store import LearningStore
            perception["learning_store"] = LearningStore().stats()
        except Exception:
            pass

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
        score += self._reason_worker_utilization(perception)
        score += self._reason_results(perception, patterns)
        score += self._reason_temporal(perception, patterns)
        score += self._reason_alerts(perception)
        score += self._reason_strategic(perception)
        score += self._reason_missions()
        score += self._reason_calibration()
        score += 5  # routine tick
        return score

    def _reason_worker_utilization(self, perception):
        """Thought 1: Worker utilization analysis."""
        score = 0
        idle = [n for n, i in perception["workers"].items() if i["state"] == "IDLE"]
        dead = [n for n, i in perception["workers"].items() if not i["alive"]]

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
                "Idle capacity available. Orchestrator may dispatch when ready.",
                f"Idle workers: {','.join(w.upper() for w in idle[:2])} -- {perception['pending_todos']} tasks queued"
            )
            score += 15
        elif idle and perception["pending_todos"] == 0:
            self.cot.think(
                f"{len(idle)} idle worker(s), 0 pending tasks",
                "Workers available but no queued work. Consider self-improvement.",
                f"Workers {','.join(w.upper() for w in idle)} ready for new assignments"
            )
            score += 15
        return score

    def _reason_results(self, perception, patterns):
        """Thought 2: Result synthesis analysis."""
        score = 0
        new_results = perception["bus_results"]
        if not new_results:
            return score

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

        if patterns["convergence"]:
            self.cot.think(
                f"Multiple workers converged on topics: {', '.join(patterns['convergence'][:3])}",
                "Convergence indicates strong signal. Cross-reference findings.",
                f"High-confidence insights on: {', '.join(patterns['convergence'][:3])}"
            )
            score += 20
        return score

    def _reason_temporal(self, perception, patterns):
        """Thought 3: Temporal pattern analysis."""
        score = 0
        if patterns["stall_pattern"]:
            self.cot.think(
                "Worker state cycling detected (IDLE->PROCESSING->IDLE) without results",
                "Worker may be receiving tasks but failing silently. Investigate.",
                "ALERT: Potential silent failure loop detected"
            )
            score += 40

        if patterns["dispatch_drought"] and perception["pending_todos"] > 0:
            # Idle workers + pending TODOs is NORMAL when orchestrator is
            # deliberately pausing (e.g. strategic pivot, busywork purge).
            # Only flag as informational, never URGENT/CRITICAL.
            self.cot.think(
                f"No dispatches in 10 minutes but {perception['pending_todos']} pending tasks",
                "Orchestrator may be strategically pausing dispatch. Inform, do not escalate.",
                f"Info: {perception['pending_todos']} pending tasks, dispatch paused"
            )
            score += 10

        if patterns["failure_rate_10m"] >= 3:
            self.cot.think(
                f"{patterns['failure_rate_10m']} failures in last 10 minutes",
                "Elevated failure rate suggests systemic issue (model drift? broken deps?).",
                f"CRITICAL: Failure rate spike -- {patterns['failure_rate_10m']} in 10min"
            )
            score += 50
        return score

    def _reason_alerts(self, perception):
        """Thought 4: Alert processing."""
        if not perception["bus_alerts"]:
            return 0
        alert_content = str(perception["bus_alerts"][0].get("content", ""))[:60]
        self.cot.think(
            f"{len(perception['bus_alerts'])} active alert(s): {alert_content}",
            "Alerts require orchestrator attention. Triage and respond.",
            f"{len(perception['bus_alerts'])} alert(s) need attention"
        )
        return 40

    def _reason_strategic(self, perception):
        """Thought 5: Strategic assessment."""
        if not perception["skynet_status"]:
            return 0
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
            return 15
        return 0

    def _reason_missions(self):
        """Thought 6: Mission continuity check."""
        stale = [m for m in self.active_missions
                 if time.time() - m.get("started", 0) > 600
                 and m.get("status") == "active"]
        if not stale:
            return 0
        names = [m.get("goal", "?")[:30] for m in stale[:2]]
        self.cot.think(
            f"{len(stale)} mission(s) running >10min: {'; '.join(names)}",
            "Long-running missions may be stalled. Check worker progress.",
            f"Check missions: {'; '.join(names)}"
        )
        return 25

    def _reason_calibration(self):
        """Thought 7: Self-calibration of prompt effectiveness."""
        if len(self.prompt_effectiveness) < 10:
            return 0
        recent = self.prompt_effectiveness[-10:]
        effective = sum(1 for _, acted in recent if acted)
        eff_rate = effective / len(recent)
        if eff_rate < 0.3:
            self.dynamic_threshold = min(self.dynamic_threshold + 5, 80)
            self.cot.think(
                f"Prompt effectiveness low: {eff_rate:.0%} of last 10 prompts acted on",
                "Raising threshold to reduce noise. Fewer, higher-quality prompts.",
                f"Self-calibrated: threshold raised to {self.dynamic_threshold}"
            )
        elif eff_rate > 0.8 and self.dynamic_threshold > 30:
            self.dynamic_threshold = max(self.dynamic_threshold - 5, 30)
        return 0

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

        Uses the built-in template generator only.
        Only generates if there's something actionable (avoids spam).
        """
        stamp = self._prompt_timestamp()

        new_results = self._filter_undelivered(perception.get("bus_results", []), "result")
        new_alerts = self._filter_undelivered(perception.get("bus_alerts", []), "alert")
        pending_todos = perception["pending_todos"]
        orch_todos = _get_pending_todo_items("orchestrator", limit=3)
        idle_workers = [n for n, i in perception["workers"].items() if i["state"] == "IDLE"]
        daemon_status = self._collect_daemon_health(perception.get("bus_alerts", []))

        status_line = self._build_status_line(
            perception, new_results, new_alerts, pending_todos, orch_todos, daemon_status)

        actions = self._build_actions(
            perception, patterns, new_results, new_alerts, pending_todos, orch_todos, idle_workers)

        if not self._has_actionable(
                new_results, new_alerts, idle_workers, pending_todos, orch_todos, patterns, daemon_status, perception):
            self.pending_prompt_state_hash = ""
            return ""

        self.pending_prompt_state_hash = self._build_prompt_state_hash(
            perception,
            patterns,
            new_results,
            new_alerts,
            pending_todos,
            orch_todos,
            daemon_status,
        )

        self._mark_delivered(new_results, "result")
        self._mark_delivered(new_alerts, "alert")

        actions.sort(key=lambda x: x[0], reverse=True)
        top_action = actions[0][1] if actions else "System needs attention."
        prompt_counter = f"({self.consecutive_prompts + 1}/{self.max_consecutive})"
        return self._format_prompt(
            stamp,
            f"Skynet Intel: {status_line} || {top_action} {prompt_counter}",
        )

    def _prompt_timestamp(self):
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _format_prompt(self, stamp, content):
        return f"[SELF-PROMPT {stamp}] {content}"

    def _try_ollama_prompt(self, perception, patterns):
        """Compatibility stub. Ollama is intentionally disabled in the self-prompt pipeline."""
        return None  # signed: consultant

    def _should_use_ollama_for_prompt(self):
        """Compatibility stub. The self-prompt pipeline no longer uses Ollama."""
        return False  # signed: consultant

    def _build_worker_status_parts(self, perception):
        """Build compact worker status string (e.g. 'A=IDLE B=PROC(45s)')."""
        parts = []
        for name, info in sorted(perception["workers"].items()):
            state = info["state"]
            model = info.get("model")
            if state == "PROCESSING" and model and model.state_since:
                elapsed = int(time.time() - model.state_since)
                label = f"STUCK({elapsed}s)" if elapsed > 180 else f"PROC({elapsed}s)"
                parts.append(f"{name[0].upper()}={label}")
            elif state == "DEAD":
                parts.append(f"{name[0].upper()}=DEAD!")
            elif state == "IDLE":
                parts.append(f"{name[0].upper()}=IDLE")
            else:
                parts.append(f"{name[0].upper()}={state[:4]}")
        return " ".join(parts)

    def _normalize_signal_text(self, text):
        """Collapse numeric drift so repeating alerts hash to the same issue family."""
        normalized = " ".join(str(text).lower().split())
        normalized = re.sub(r"\d+", "#", normalized)
        return normalized[:160]

    def _message_identity(self, message, include_message_id=False):
        if include_message_id and message.get("id"):
            return f"id:{message['id']}"
        sender = str(message.get("sender", "?")).lower()
        topic = str(message.get("topic", "?")).lower()
        msg_type = str(message.get("type", "?")).lower()
        content = self._normalize_signal_text(message.get("content", ""))
        return f"{sender}|{topic}|{msg_type}|{content}"

    def _build_prompt_state_hash(
        self,
        perception,
        patterns,
        new_results,
        new_alerts,
        pending_todos,
        orch_todos,
        daemon_status,
    ):
        workers = {
            name: {
                "state": info.get("state", "UNKNOWN"),
                "alive": bool(info.get("alive", False)),
            }
            for name, info in sorted(perception.get("workers", {}).items())
        }
        payload = {
            "workers": workers,
            "results": sorted(
                self._message_identity(m, include_message_id=True)
                for m in new_results
            ),
            "alerts": sorted({self._message_identity(m) for m in new_alerts}),
            "pending_todos": int(pending_todos),
            "pending_tasks": int(perception.get("pending_tasks", 0)),
            "orch_todos": [
                str(t.get("id") or t.get("task") or t.get("title") or "")[:120]
                for t in orch_todos
            ],
            "daemon_status": daemon_status,
            "stall_pattern": bool(patterns.get("stall_pattern", False)),
            "failure_rate_10m": int(patterns.get("failure_rate_10m", 0)),
            "critical": [
                self._normalize_signal_text(c)
                for c in self.cot.critical_conclusions()[:3]
            ],
        }
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hashlib.md5(encoded).hexdigest()[:12]  # signed: consultant

    def _collect_daemon_health(self, alerts):
        """Check daemon health via live probes, not just file reads.
        Truth principle: verify each daemon is actually responsive."""
        daemon_issues = []

        # --- Live HTTP probes for critical daemons (Truth: verify, don't trust files) ---
        def _http_probe(url, timeout=2):
            """Return True if HTTP endpoint responds with 2xx."""
            try:
                req = urllib.request.Request(url, method="GET")
                resp = urllib.request.urlopen(req, timeout=timeout)
                return 200 <= resp.status < 300
            except Exception:
                return False

        # Skynet backend (port 8420)
        if not _http_probe("http://localhost:8420/status"):
            daemon_issues.append("skynet=unresponsive")

        # GOD Console (port 8421)
        if not _http_probe("http://localhost:8421/health"):
            # Fallback: try root endpoint
            if not _http_probe("http://localhost:8421/"):
                daemon_issues.append("god_console=unresponsive")

        # Watchdog file as supplementary signal (not primary truth source)
        watchdog_status = _load_json(DATA_DIR / "watchdog_status.json")
        if watchdog_status and isinstance(watchdog_status, dict):
            for svc in ("sse_daemon",):  # skynet/god_console verified by HTTP above
                svc_status = watchdog_status.get(svc, "unknown")
                if svc_status not in ("ok", "unknown"):
                    daemon_issues.append(f"{svc}={svc_status}")
        # signed: delta

        # Check monitor health: content timestamp > file mtime > PID liveness.
        # Threshold 300s accounts for adaptive slowdown (monitor runs at 60s
        # when all workers idle). PID check prevents false stale when monitor
        # process is alive but health file hasn't been updated yet.
        monitor_health_file = DATA_DIR / "monitor_health.json"
        monitor_pid_file = DATA_DIR / "monitor.pid"
        MONITOR_STALE_THRESHOLD = 300  # 5 min -- monitor writes every 30-60s

        def _monitor_pid_alive():
            """Check if monitor.pid process is actually running."""
            try:
                if monitor_pid_file.exists():
                    pid = int(monitor_pid_file.read_text().strip())
                    import psutil
                    proc = psutil.Process(pid)
                    return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
            except Exception:
                pass
            return False

        if monitor_health_file.exists():
            try:
                mdata = _load_json(monitor_health_file)
                content_age = None
                if mdata and isinstance(mdata, dict):
                    # Try content "updated" field first (more reliable than mtime)
                    updated_str = mdata.get("updated", "")
                    if updated_str:
                        try:
                            from datetime import datetime as _dt
                            updated_dt = _dt.fromisoformat(
                                updated_str.replace("Z", "+00:00"))
                            content_age = time.time() - updated_dt.timestamp()
                        except Exception:
                            pass
                    # Fall back to "latest.epoch" field
                    if content_age is None:
                        epoch = (mdata.get("latest") or {}).get("epoch")
                        if epoch:
                            content_age = time.time() - float(epoch)

                age = content_age
                if age is None:
                    age = time.time() - monitor_health_file.stat().st_mtime

                if age > MONITOR_STALE_THRESHOLD:
                    # File is stale -- but is the process still alive?
                    if _monitor_pid_alive():
                        # Process alive but file stale: likely just started or
                        # slow cycle. Don't raise full alarm.
                        daemon_issues.append("monitor=pid_alive_file_stale")
                    else:
                        daemon_issues.append("monitor=stale")
            except Exception:
                pass
        elif monitor_pid_file.exists():
            if _monitor_pid_alive():
                pass  # PID alive, health file not yet created -- normal at startup
            else:
                daemon_issues.append("monitor=no_health")
        # signed: delta

        for a in alerts:
            content = str(a.get("content", "")).lower()
            if "daemon" in content and ("dead" in content or "down" in content or "stale" in content):
                daemon_issues.append("alert")
                break

        return ",".join(daemon_issues) if daemon_issues else "verified_ok"
        # signed: delta

    def _build_status_line(self, perception, new_results, new_alerts, pending_todos, orch_todos, daemon_status):
        """Assemble the status line from components."""
        status_parts = [self._build_worker_status_parts(perception)]
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
        return " | ".join(status_parts)

    def _build_actions(self, perception, patterns, new_results, new_alerts, pending_todos, orch_todos, idle_workers):
        """Build prioritized action list for the prompt."""
        actions = []

        if self.cot.has_conclusions():
            for c in self.cot.critical_conclusions()[:1]:
                actions.append((100, f"CRITICAL: {c.strip()}"))

        if orch_todos:
            top = orch_todos[0]
            pri = str(top.get("priority", "normal")).upper()
            task = str(top.get("task", "pending orchestrator task"))[:160]
            actions.append((97, f"EXECUTE ORCH TODO [{pri}]: {task}"))
            if len(orch_todos) > 1:
                n = orch_todos[1]
                actions.append((96, f"NEXT TODO [{str(n.get('priority','normal')).upper()}]: {str(n.get('task',''))[:120]}"))

        if patterns["stall_pattern"]:
            actions.append((90, "Worker stall detected -- investigate worker cycling."))
        if patterns["failure_rate_10m"] >= 3:
            actions.append((85, f"Failure spike: {patterns['failure_rate_10m']} failures in last 10min."))
        if patterns["dispatch_drought"] and pending_todos > 0:
            actions.append((30, f"Info: {pending_todos} pending tasks, dispatch paused by orchestrator."))

        if new_results:
            senders = list({r.get("sender", "?").title() for r in new_results})
            actions.append((70, f"{len(new_results)} result(s) from {','.join(senders[:3])} ready for synthesis."))

        if idle_workers and pending_todos > 0:
            actions.append((40, f"{len(idle_workers)} idle worker(s), {pending_todos} tasks queued."))
        elif idle_workers and not pending_todos and not new_results:
            actions.append((20, f"{len(idle_workers)} idle worker(s), no pending work."))

        if new_alerts:
            actions.append((75, f"ALERT: {str(new_alerts[0].get('content', ''))[:80]}"))

        return actions

    def _has_actionable(self, new_results, new_alerts, idle_workers, pending_todos, orch_todos, patterns, daemon_status, perception):
        """Determine if there's anything worth prompting about.

        Idle workers with pending tasks is NORMAL, not actionable on its own.
        Only genuine system issues or unprocessed results/alerts trigger prompts.
        """
        critical_conclusions = bool(self.cot.critical_conclusions())
        return (  # signed: consultant
            len(new_results) > 0
            or len(new_alerts) > 0
            or len(orch_todos) > 0
            or patterns["stall_pattern"]
            or patterns["failure_rate_10m"] >= 3
            or any(not i["alive"] for i in perception["workers"].values())
            or critical_conclusions
            or daemon_status != "verified_ok"
        )

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

        sections = [s for s in self._gather_intelligence() if s]
        if not sections:
            log("Boot prompt suppressed: nothing actionable")
            return  # signed: consultant

        prompt = self._format_prompt(
            self._prompt_timestamp(),
            "Skynet Intel: " + " | ".join(sections),
        )

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
        health = self._build_health_payload(now=now)
        try:
            _save_json(HEALTH_FILE, health)
        except Exception:
            pass  # never crash the daemon for a health write

    def _build_health_payload(self, now=None, orch_hwnd=None):
        now = time.time() if now is None else now
        orch_hwnd = _get_orch_hwnd() if orch_hwnd is None else orch_hwnd
        return {
            "daemon": DAEMON_NAME,
            "daemon_version": DAEMON_VERSION,
            "skynet_version": _get_skynet_version() or "unavailable",
            "cycles": self.cycles,
            "sent": self.prompts_sent,
            "failed": self.prompts_failed,
            "last_sent_timestamp": getattr(self, 'last_prompt_time', 0.0),
            "orchestrator_hwnd": orch_hwnd,
            "pid": os.getpid(),
            "uptime_s": int(now - getattr(self, '_start_time', now)),
            "timestamp": datetime.now().isoformat(),
        }

    def _report_health(self):
        """Post health status to bus periodically (separate from per-cycle file write)."""
        now = time.time()
        if now - self.last_health_report < HEALTH_REPORT_INTERVAL:
            return
        self.last_health_report = now
        health = self._build_health_payload(now=now)
        _post_bus("orchestrator", "daemon_health",
                  _versioned_signal(
                      "SELF_PROMPT_HEALTH",
                      f"cycles={self.cycles} sent={self.prompts_sent} failed={self.prompts_failed} "
                      f"skynet_v={health['skynet_version']}"
                  ))
        log(f"Health report posted (cycles={self.cycles}, sent={self.prompts_sent})")

    def _scan_triggers(self):
        """CHAIN-OF-THOUGHT trigger system. Returns (should_prompt, reasons, prompt_text, score)."""
        orch_hwnd = _get_orch_hwnd()
        if not orch_hwnd or not _is_window_alive(orch_hwnd):
            return False, ["orch_window_dead"], "", 0

        if self._is_dispatch_active():
            return False, ["dispatch_active"], "", 0
        if self._is_boot_in_progress():
            return False, ["boot_in_progress"], "", 0

        orch_state = _get_worker_state(orch_hwnd)
        if orch_state == "TYPING":
            return False, ["orch_typing"], "", 0

        # Full Chain-of-Thought Pipeline
        perception = self._perceive()
        patterns = self._remember(perception)
        score = self._reason(perception, patterns)
        predictions = self._predict(perception, patterns)
        reasons = self._extract_cot_reasons()

        total_score = self.accumulated_score + score
        should_prompt = total_score >= self.dynamic_threshold
        urgent_bypass = self._check_urgent_bypass(reasons, total_score)

        prompt_text = ""
        if should_prompt or urgent_bypass:
            prompt_text = self._synthesize_prompt(perception, patterns, predictions, total_score)
            if not prompt_text:
                self.accumulated_score = total_score
                return False, ["nothing_actionable"], "", total_score
            self.accumulated_score = 0
            bypass_tag = " [URGENT_BYPASS]" if (urgent_bypass and not should_prompt) else ""
            self._record_event(EVT_PROMPT, "system", f"score={total_score} reasons={','.join(reasons[:3])}{bypass_tag}")
            self._save_persistent_state()
        else:
            self.accumulated_score = total_score

        return (should_prompt or urgent_bypass), reasons, prompt_text, total_score

    def _extract_cot_reasons(self):
        """Extract reason tags from chain-of-thought conclusions."""
        reasons = []
        keyword_map = {
            "CRITICAL": "critical_finding",
            "URGENT": "urgent_finding",
            "Dispatch": "dispatch_opportunity",
            "dispatch": "dispatch_opportunity",
            "Synthesize": "results_ready",
            "failure": "failure_detected",
            "FAILURE": "failure_detected",
            "anomaly": "anomaly_detected",
            "ANOMALY": "anomaly_detected",
        }
        for step in self.cot.steps:
            conclusion = step.get("conclusion", "")
            for keyword, reason in keyword_map.items():
                if keyword in conclusion:
                    reasons.append(reason)
                    break
        return reasons or ["routine_tick"]

    def _check_urgent_bypass(self, reasons, total_score):
        """Determine if conditions warrant bypassing the score threshold."""
        if "critical_finding" in reasons or "urgent_finding" in reasons:
            return True
        if "results_ready" in reasons and total_score >= 30:
            return True
        if "failure_detected" in reasons:
            return True
        return False

    def _deliver_queued_directives(self):
        """Check orch_queue.json for pending directives and type them into the orchestrator."""
        try:
            if not ORCH_QUEUE_FILE.exists():
                return 0
            data = json.loads(ORCH_QUEUE_FILE.read_text(encoding="utf-8"))
            pending = [e for e in data.get("queue", []) if e.get("status") == "pending"]
            if not pending:
                return 0

            orch_hwnd = _get_orch_hwnd()
            if not orch_hwnd or not _is_window_alive(orch_hwnd):
                return 0
            if _get_worker_state(orch_hwnd) == "TYPING":
                return 0

            delivered = 0
            for entry in pending:
                ok = self._deliver_one_directive(entry, orch_hwnd, data)
                if ok:
                    delivered += 1
                else:
                    break

            if delivered > 0:
                ORCH_QUEUE_FILE.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            return delivered
        except Exception as e:
            log(f"Queued directive delivery error: {e}", "ERROR")
            return 0

    def _deliver_one_directive(self, entry, orch_hwnd, data):
        """Deliver a single queued directive. Returns True on success."""
        content = entry.get("content", "")
        if not content:
            return False

        sender = entry.get("sender", "?")
        priority = str(entry.get("priority", "normal")).upper()
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
            log(f"Queued directive DELIVERED (msg_id={entry.get('msg_id', '')})")
            self.last_prompt_time = time.time()
            time.sleep(2)
        else:
            log("Queued directive delivery FAILED", "ERROR")
        return ok

    def check_and_prompt(self, deliver_queue_first=True):
        """Single check cycle with priority scoring and anti-spam. Returns True if prompt was sent."""
        self.cycles += 1
        now = time.time()

        self._report_health()

        if deliver_queue_first:
            queued_delivered = self._deliver_queued_directives()
            if queued_delivered > 0:
                log(f"Delivered {queued_delivered} queued directive(s)")
                return True

        live_snapshot = self._fetch_worker_status_snapshot()
        if not self._refresh_all_idle_window(now, live_snapshot):
            return False

        if not self._all_idle_window_ready(now):
            return False

        effective_gap = max(1.0, float(MIN_PROMPT_GAP))
        if now - self.last_prompt_time < effective_gap:
            return False

        # Consecutive prompt limiter
        if self._orchestrator_took_action():
            if self.consecutive_prompts > 0:
                log(f"Orchestrator took action -- resetting consecutive counter ({self.consecutive_prompts} -> 0)")
            self.consecutive_prompts = 0
            self.last_cycle_complete_time = 0.0
        if self._final_shot_cooldown_active(now):
            return False

        should, reasons, prompt, total_score = self._scan_triggers()
        if not should:
            return False

        orch_hwnd = _get_orch_hwnd()
        if not orch_hwnd:
            log("No orchestrator HWND", "ERROR")
            return False

        if self._check_orch_typing(orch_hwnd):
            return False

        if self._is_duplicate_prompt(prompt, now):
            return False

        pre_fire_now = time.time()
        pre_fire_snapshot = self._fetch_worker_status_snapshot()
        if not self._refresh_all_idle_window(pre_fire_now, pre_fire_snapshot):
            log(
                "Aborting self-prompt: live worker state changed before fire "
                f"({self._format_worker_snapshot(pre_fire_snapshot)})",
                "WARN",
            )
            return False
        if not self._all_idle_window_ready(pre_fire_now):
            log("Aborting self-prompt: all-idle window not yet satisfied at fire time", "WARN")
            return False

        log(f"TRIGGERING (score={total_score}): {', '.join(reasons)}")
        log(f"Prompt: {prompt[:120]}...")

        ok = _send_self_prompt(orch_hwnd, prompt)
        self._record_prompt_delivery(ok, now, prompt, reasons, total_score)
        return ok

    def _check_orch_typing(self, orch_hwnd):
        """Check if orchestrator is actively typing (collision guard)."""
        try:
            sys.path.insert(0, str(ROOT / "tools"))
            from uia_engine import get_engine
            orch_state = get_engine().get_state(orch_hwnd)
            if orch_state == "TYPING":
                log("Orchestrator TYPING -- skipping prompt (collision guard)")
                return True
            if orch_state == "PROCESSING":
                log("Orchestrator PROCESSING -- queuing prompt (VS Code will queue)")
        except Exception as e:
            log(f"State check failed: {e} -- prompting anyway", "WARN")
        return False

    def _is_duplicate_prompt(self, prompt, now):
        """Anti-spam: suppress identical text quickly and repeated no-change states longer."""
        prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:12]
        if prompt_hash == self.last_prompt_hash and (now - self.last_prompt_hash_time) < 60:
            log(f"Duplicate prompt suppressed (hash={prompt_hash})")
            return True
        state_hash = self.pending_prompt_state_hash
        if (
            state_hash
            and state_hash == self.last_prompt_state_hash
            and (now - self.last_prompt_state_hash_time) < REPEATED_STATE_SUPPRESSION_S
        ):
            remaining = int(REPEATED_STATE_SUPPRESSION_S - (now - self.last_prompt_state_hash_time))
            log(f"Repeated prompt state suppressed (hash={state_hash}, remaining={remaining}s)")
            return True
        return False

    def _record_prompt_delivery(self, ok, now, prompt, reasons, total_score):
        """Record prompt delivery outcome and update tracking state."""
        prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:12]
        self.last_prompt_time = now
        self.last_prompt_hash = prompt_hash
        self.last_prompt_hash_time = now

        _append_log({
            "timestamp": datetime.now().isoformat(),
            "trigger_reasons": reasons,
            "score": total_score,
            "prompt": prompt[:200],
            "delivered": ok,
        })
        _update_last_action("self_prompt_sent")

        if ok:
            self.prompts_sent += 1
            self.consecutive_prompts += 1
            self.all_idle_since = now
            self.last_prompt_state_hash = self.pending_prompt_state_hash
            self.last_prompt_state_hash_time = now
            if self.consecutive_prompts >= self.max_consecutive:
                self.last_cycle_complete_time = now
            log(f"Prompt DELIVERED (consecutive {self.consecutive_prompts}/{self.max_consecutive})")
            _post_bus("orchestrator", "self_prompt",
                      f"Self-prompt sent (score={total_score}, {self.consecutive_prompts}/{self.max_consecutive}): {', '.join(reasons)}")
        else:
            self.prompts_failed += 1
            log("Prompt FAILED to deliver", "ERROR")
        if not ok:
            self.pending_prompt_state_hash = ""

    def _final_shot_cooldown_active(self, now):
        """After the last shot fires, wait MIN_PROMPT_GAP before starting a new cycle."""
        if self.consecutive_prompts < self.max_consecutive:
            return False

        if not self.last_cycle_complete_time:
            self.last_cycle_complete_time = self.last_prompt_time or now

        cooldown_s = max(1.0, float(MIN_PROMPT_GAP))
        remaining = cooldown_s - (now - self.last_cycle_complete_time)
        if remaining > 0:
            log(
                f"Consecutive limit reached ({self.consecutive_prompts}/{self.max_consecutive}) -- "
                f"cooldown {int(remaining)}s since final shot"
            )
            return True

        log("Final-shot cooldown elapsed -- resetting self-prompt shot counter")
        self.consecutive_prompts = 0
        self.last_cycle_complete_time = 0.0
        return False

    def _fetch_worker_status_snapshot(self):
        """Fetch live worker state from registered HWNDs using UIA truth.

        This is stricter than backend /status. If a worker HWND is missing,
        dead, or unreadable, the gate fails closed with UNKNOWN/DEAD.
        """
        snapshot = {name: "UNKNOWN" for name in REQUIRED_WORKERS}
        try:
            workers = _load_workers()
            if not workers:
                return snapshot

            worker_map = {
                str(w.get("name", "")).lower(): w
                for w in workers
                if isinstance(w, dict)
            }
            for name in REQUIRED_WORKERS:
                w = worker_map.get(name)
                if not w:
                    snapshot[name] = "UNKNOWN"
                    continue
                hwnd = int(w.get("hwnd", 0) or 0)
                if not hwnd:
                    snapshot[name] = "UNKNOWN"
                    continue
                if not _is_window_alive(hwnd):
                    snapshot[name] = "DEAD"
                    continue
                state = str(_get_worker_state(hwnd) or "UNKNOWN").upper()
                snapshot[name] = state
            return snapshot
        except Exception:
            return snapshot

    def _snapshot_all_workers_idle(self, snapshot):
        return all(str(snapshot.get(name, "UNKNOWN")).upper() == "IDLE" for name in REQUIRED_WORKERS)

    def _format_worker_snapshot(self, snapshot):
        ordered = sorted(REQUIRED_WORKERS)
        return " ".join(f"{name[0].upper()}={str(snapshot.get(name, 'UNKNOWN')).upper()}" for name in ordered)

    def _refresh_all_idle_window(self, now, snapshot=None):
        snapshot = snapshot or self._fetch_worker_status_snapshot()
        if self._snapshot_all_workers_idle(snapshot):
            if not self.all_idle_since:
                self.all_idle_since = now
                log(f"All workers IDLE -- starting quiet window ({int(ALL_IDLE_INTERVAL)}s)")
            return True

        if self.all_idle_since:
            quiet_s = int(now - self.all_idle_since)
            log(
                f"All-idle quiet window reset after {quiet_s}s "
                f"({self._format_worker_snapshot(snapshot)})"
            )
        self.all_idle_since = 0.0
        return False

    def _all_idle_window_ready(self, now):
        if not self.all_idle_since:
            return False
        return (now - self.all_idle_since) >= max(1.0, float(ALL_IDLE_INTERVAL))

    def run(self):
        """Main daemon loop -- status-based with configurable cadence."""
        self._start_time = time.time()
        log(f"Self-prompt daemon v{DAEMON_VERSION} starting (status-based, {LOOP_INTERVAL}s poll)")
        log(f"Config: poll={LOOP_INTERVAL}s gap={MIN_PROMPT_GAP}s threshold={PROMPT_THRESHOLD} idle_thresh={IDLE_WORKER_THRESHOLD}s")
        _post_bus("orchestrator", "monitor_alert",
                  _versioned_signal(
                      "SELF_PROMPT_ONLINE",
                      f"Orchestrator heartbeat daemon started (status-based polling, skynet_v={_get_skynet_version() or 'unavailable'})"
                  ))

        self._wait_for_boot_completion()

        if BOOT_PROMPT_ENABLED:
            try:
                self._boot_prompt()
            except Exception as e:
                log(f"Boot prompt failed: {e}", "ERROR")
        else:
            self.last_prompt_time = time.time()
            log(f"Boot prompt disabled -- priming quiet period for {int(MIN_PROMPT_GAP)}s")

        try:
            while True:
                self._main_loop_cycle()
                time.sleep(max(1, float(LOOP_INTERVAL)))
        except KeyboardInterrupt:
            log("Shutting down (Ctrl+C)")
        finally:
            _post_bus("orchestrator", "monitor_alert",
                      _versioned_signal("SELF_PROMPT_OFFLINE", "Heartbeat daemon stopped"))
            if PID_FILE.exists():
                try:
                    PID_FILE.unlink()
                except Exception:
                    pass

    def _wait_for_boot_completion(self):
        """Wait for skynet_start.py boot phases to finish before touching orchestrator."""
        boot_wait = 0
        while self._is_boot_in_progress() and boot_wait < 120:
            time.sleep(5)
            boot_wait += 5
            if boot_wait % 15 == 0:
                log(f"Waiting for boot to complete ({boot_wait}s)...")
        if boot_wait > 0:
            log(f"Boot completed after {boot_wait}s wait", "OK")
        else:
            time.sleep(5)

    def _main_loop_cycle(self):
        """Single iteration of the main daemon loop."""
        try:
            _load_config_overrides()
            self._write_health_file()

            queued_delivered = self._deliver_queued_directives()
            if queued_delivered > 0:
                log(f"Delivered {queued_delivered} queued directive(s)")
            else:
                self.check_and_prompt(deliver_queue_first=False)
        except Exception as e:
            log(f"Check failed: {e}", "ERROR")


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


def _action_status():
    """Show last self-prompt log entry and daemon PID."""
    log_data = _load_json(LOG_FILE)
    health = _load_json(HEALTH_FILE) or {}
    print(f"{DAEMON_NAME} v{health.get('daemon_version', DAEMON_VERSION)}")
    print(f"Skynet version: {health.get('skynet_version') or _get_skynet_version() or 'unavailable'}")
    if log_data and isinstance(log_data, list) and len(log_data) > 0:
        print(json.dumps(log_data[-1], indent=2))
        print(f"\nTotal prompts logged: {len(log_data)}")
    else:
        print("No self-prompt log found.")
    pid = _check_existing()
    print(f"Daemon running: PID {pid}" if pid else "Daemon not running.")


def _action_version():
    print(f"{DAEMON_NAME} v{DAEMON_VERSION} (Skynet v{_get_skynet_version() or 'unavailable'})")


def _action_start():
    """Start the self-prompt daemon if not already running."""
    existing = _check_existing()
    if existing:
        print(f"Already running (PID {existing}). Use 'status' to check.")
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    # ── atexit + signal handlers for PID cleanup ──  # signed: alpha
    import atexit
    def _cleanup_pid():
        try:
            if PID_FILE.exists() and int(PID_FILE.read_text().strip()) == os.getpid():
                PID_FILE.unlink()
        except Exception:
            pass
    atexit.register(_cleanup_pid)

    def _sigterm_handler(signum, frame):
        log(f"Received signal {signum} -- requesting graceful shutdown")
        raise KeyboardInterrupt  # triggers existing except/finally blocks
    signal.signal(signal.SIGTERM, _sigterm_handler)
    try:
        signal.signal(signal.SIGBREAK, _sigterm_handler)  # Windows Ctrl+Break
    except (AttributeError, OSError):
        pass  # signed: alpha

    log(f"Self-prompt daemon v{DAEMON_VERSION} PID {os.getpid()}")
    _update_last_action("daemon_start")
    SelfPromptDaemon().run()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skynet Self-Prompt -- Orchestrator Heartbeat")
    parser.add_argument("action", nargs="?", default="status",
                        choices=["start", "status", "once", "stop", "version"],
                        help="start=daemon, status=last prompt, once=single check, stop=show PID, version=show daemon version")
    args = parser.parse_args()

    actions = {
        "status": _action_status,
        "version": _action_version,
        "once": lambda: SelfPromptDaemon().check_and_prompt(),
        "stop": lambda: print(f"Self-prompt daemon running as PID {p}." if (p := _check_existing()) else "Daemon not running."),
        "start": _action_start,
    }
    actions[args.action]()


if __name__ == "__main__":
    main()
