"""
GOD Console — The Human Authority Layer.

GOD is the human operator. GOD does NOT do mundane work. GOD only:
1. Approves/Rejects critical decisions (deployments, deletions, external API calls)
2. Sets directives — high-level goals the orchestrator breaks down
3. Sees everything — full system awareness (agent status, tasks, errors, resources)
4. Overrides — can halt any agent, reprioritize tasks, or force a strategy

Every security-sensitive, destructive, or external operation MUST pass through
GOD approval before execution. Agents queue requests, GOD decides.

Risk Classification:
    CRITICAL — production, external APIs, deletion, auth credentials
    HIGH     — core code changes, new deployments
    MEDIUM   — test runs, builds, file writes
    LOW      — reads, analysis, research
"""

import sqlite3
import json
import time
import threading
import platform
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from uuid import uuid4

logger = logging.getLogger(__name__)

# Paths
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "god_console.db"
QUEUE_PATH = DATA_DIR / "agent_queues" / "god_queue.json"

# Agent queue dir (for reading live status of other agents)
AGENT_QUEUE_DIR = DATA_DIR / "agent_queues"


# ============================================================================
# ENUMS
# ============================================================================

class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class DirectiveStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class AgentAction(str, Enum):
    HALT = "halt"
    RESUME = "resume"
    REPRIORITIZE = "reprioritize"
    RESTART = "restart"


# ============================================================================
# RISK CLASSIFIER
# ============================================================================

# Keywords that trigger each risk level
RISK_KEYWORDS: Dict[RiskLevel, List[str]] = {
    RiskLevel.CRITICAL: [
        "production", "prod", "deploy_prod", "delete", "remove", "drop",
        "external_api", "api_call", "credential", "auth", "secret",
        "password", "token", "key_rotation", "database_drop", "rm -rf",
        "send_email", "payment", "billing", "webhook_external",
    ],
    RiskLevel.HIGH: [
        "deploy", "deployment", "code_change_core", "core_module",
        "migration", "schema_change", "rollback", "release",
        "config_change", "infrastructure", "scaling",
    ],
    RiskLevel.MEDIUM: [
        "test_run", "build", "compile", "file_write", "write_file",
        "install", "pip_install", "npm_install", "cache_clear",
        "restart_service", "log_rotate",
    ],
    RiskLevel.LOW: [
        "read", "analyze", "research", "search", "query", "list",
        "status", "health_check", "log_read", "report", "scan",
    ],
}


def classify_risk(action: str) -> RiskLevel:
    """Classify an action string into a risk level using keyword matching."""
    action_lower = action.lower()
    for level in [RiskLevel.CRITICAL, RiskLevel.HIGH, RiskLevel.MEDIUM, RiskLevel.LOW]:
        for keyword in RISK_KEYWORDS[level]:
            if keyword in action_lower:
                return level
    return RiskLevel.MEDIUM  # default to MEDIUM if unrecognized


# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass
class PendingApproval:
    """An action queued for GOD's approval."""
    id: str
    action: str
    agent_id: str
    risk_level: str  # RiskLevel value
    detail: str
    timestamp: float
    status: str = ApprovalStatus.PENDING.value
    reviewed_at: Optional[float] = None
    rejection_reason: Optional[str] = None
    auto_expire_seconds: int = 3600  # 1 hour default

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PendingApproval":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PendingApproval":
        return cls(**dict(row))

    @property
    def is_expired(self) -> bool:
        if self.status != ApprovalStatus.PENDING.value:
            return False
        return (time.time() - self.timestamp) > self.auto_expire_seconds

    @property
    def age_display(self) -> str:
        age = time.time() - self.timestamp
        if age < 60:
            return f"{int(age)}s ago"
        elif age < 3600:
            return f"{int(age / 60)}m ago"
        else:
            return f"{age / 3600:.1f}h ago"


@dataclass
class GodDirective:
    """A high-level goal set by GOD for the orchestrator to break down."""
    id: str
    goal: str
    priority: int  # 1 (highest) to 10 (lowest)
    created_at: float
    status: str = DirectiveStatus.ACTIVE.value
    sub_tasks: List[str] = field(default_factory=list)
    completed_at: Optional[float] = None
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sub_tasks"] = json.dumps(d["sub_tasks"])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "GodDirective":
        d = d.copy()
        if isinstance(d.get("sub_tasks"), str):
            d["sub_tasks"] = json.loads(d["sub_tasks"])
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class AgentOverride:
    """Record of a GOD override action on an agent."""
    id: str
    agent_id: str
    action: str  # AgentAction value
    reason: str
    timestamp: float
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["metadata"] = json.dumps(d["metadata"])
        return d


# ============================================================================
# SYSTEM AWARENESS — Full visibility for GOD
# ============================================================================

class SystemAwareness:
    """Collects system-wide context for GOD visibility."""

    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path
        self._start_time = time.time()

    def get_agent_health(self) -> Dict[str, Any]:
        """Get status of all agents by reading their live queue files."""
        agents = {}
        if not AGENT_QUEUE_DIR.exists():
            return agents

        for f in AGENT_QUEUE_DIR.glob("*_live.json"):
            agent_name = f.stem.replace("_live", "")
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                agents[agent_name] = {
                    "status": data.get("status", "unknown"),
                    "tasks_completed": data.get("tasks_completed", 0),
                    "current_task": data.get("current_task"),
                    "file": str(f),
                }
            except (json.JSONDecodeError, OSError):
                agents[agent_name] = {"status": "error_reading", "file": str(f)}

        return agents

    def get_recent_errors(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get last N errors across all agents from the database."""
        errors = []
        try:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("""
                    SELECT * FROM system_errors
                    ORDER BY timestamp DESC LIMIT ?
                """, (limit,))
                for row in cursor:
                    errors.append(dict(row))
        except sqlite3.OperationalError:
            pass  # Table may not exist yet
        return errors

    def get_task_history(self, limit: int = 100) -> Dict[str, Any]:
        """Get completed tasks with aggregate stats."""
        history: Dict[str, Any] = {"tasks": [], "total": 0, "success": 0, "failed": 0}
        try:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("""
                    SELECT * FROM task_log
                    ORDER BY completed_at DESC LIMIT ?
                """, (limit,))
                for row in cursor:
                    task = dict(row)
                    history["tasks"].append(task)
                    history["total"] += 1
                    if task.get("success"):
                        history["success"] += 1
                    else:
                        history["failed"] += 1
        except sqlite3.OperationalError:
            pass
        return history

    def get_resource_usage(self) -> Dict[str, Any]:
        """Get CPU, memory, disk for the system. Best-effort via psutil."""
        usage: Dict[str, Any] = {
            "platform": platform.platform(),
            "uptime_seconds": time.time() - self._start_time,
        }
        try:
            import psutil
            usage["cpu_percent"] = psutil.cpu_percent(interval=0.5)
            mem = psutil.virtual_memory()
            usage["memory"] = {
                "total_gb": round(mem.total / (1024 ** 3), 2),
                "used_gb": round(mem.used / (1024 ** 3), 2),
                "percent": mem.percent,
            }
            disk = psutil.disk_usage(str(DATA_DIR))
            usage["disk"] = {
                "total_gb": round(disk.total / (1024 ** 3), 2),
                "used_gb": round(disk.used / (1024 ** 3), 2),
                "percent": disk.percent,
            }
        except ImportError:
            usage["note"] = "psutil not installed — install for detailed metrics"
        return usage

    def get_file_changes(self, hours: int = 24) -> List[Dict[str, Any]]:
        """Get recent file modifications in the project directory."""
        changes = []
        cutoff = time.time() - (hours * 3600)
        project_root = Path(r"D:\Prospects\ScreenMemory")

        for pattern in ["core/*.py", "models/*.py", "*.py", "config.json"]:
            for f in project_root.glob(pattern):
                try:
                    mtime = f.stat().st_mtime
                    if mtime > cutoff:
                        changes.append({
                            "path": str(f.relative_to(project_root)),
                            "modified": datetime.fromtimestamp(mtime).isoformat(),
                            "size_bytes": f.stat().st_size,
                        })
                except OSError:
                    continue

        changes.sort(key=lambda c: c["modified"], reverse=True)
        return changes

    # ------------------------------------------------------------------
    # OMNISCIENCE LAYER — added methods for full GOD awareness
    # ------------------------------------------------------------------

    def get_process_map(self) -> Dict[str, Any]:
        """Map all Python processes related to ScreenMemory."""
        processes: Dict[str, Any] = {}
        try:
            import psutil
            keywords = ["ScreenMemory", "dashboard", "agent_worker", "orchestrator"]
            for proc in psutil.process_iter(["pid", "name", "cmdline", "cpu_percent",
                                             "memory_info", "create_time"]):
                try:
                    info = proc.info
                    cmdline = " ".join(info.get("cmdline") or [])
                    if not any(kw.lower() in cmdline.lower() for kw in keywords):
                        continue
                    pid = info["pid"]
                    mem_mb = round((info.get("memory_info") and info["memory_info"].rss or 0) / (1024 ** 2), 1)
                    uptime = round(time.time() - (info.get("create_time") or time.time()), 1)
                    snippet = cmdline[:120]
                    label = f"pid_{pid}"
                    for kw in keywords:
                        if kw.lower() in cmdline.lower():
                            label = kw
                            break
                    if label in processes:
                        label = f"{label}_{pid}"
                    processes[label] = {
                        "pid": pid,
                        "cmdline_snippet": snippet,
                        "cpu_percent": info.get("cpu_percent", 0.0),
                        "memory_mb": mem_mb,
                        "uptime_seconds": uptime,
                    }
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except ImportError:
            processes["_error"] = {"pid": 0, "cmdline_snippet": "psutil not installed",
                                   "cpu_percent": 0, "memory_mb": 0, "uptime_seconds": 0}
        except Exception as exc:
            processes["_error"] = {"pid": 0, "cmdline_snippet": str(exc)[:120],
                                   "cpu_percent": 0, "memory_mb": 0, "uptime_seconds": 0}
        return processes

    def get_live_agent_status(self) -> Dict[str, Any]:
        """Read ALL files in agent_queues/ for a complete agent picture."""
        status: Dict[str, Any] = {
            "agents": {},
            "pending_queues": {},
            "results": {},
            "orch_thinking": None,
            "god_queue": [],
        }
        if not AGENT_QUEUE_DIR.exists():
            return status

        try:
            for f in AGENT_QUEUE_DIR.iterdir():
                try:
                    if not f.is_file() or f.suffix != ".json":
                        continue
                    data = json.loads(f.read_text(encoding="utf-8"))
                    name = f.stem

                    if name.endswith("_live"):
                        agent = name.replace("_live", "")
                        status["agents"][agent] = data
                    elif name.endswith("_queue"):
                        agent = name.replace("_queue", "")
                        tasks = data if isinstance(data, list) else data.get("tasks", [])
                        status["pending_queues"][agent] = {
                            "count": len(tasks),
                            "tasks": tasks[:5],
                        }
                    elif name.endswith("_result"):
                        agent = name.replace("_result", "")
                        results = data if isinstance(data, list) else [data]
                        status["results"][agent] = {
                            "count": len(results),
                            "latest": results[-1] if results else None,
                        }
                    elif name == "orch_thinking":
                        status["orch_thinking"] = data
                    elif name == "god_queue":
                        items = data if isinstance(data, list) else data.get("pending", [])
                        status["god_queue"] = items
                    # Other files (feedback, etc.) are informational
                except (json.JSONDecodeError, OSError):
                    continue
        except Exception:
            pass
        return status

    def get_architecture_health(self) -> Dict[str, Any]:
        """Check if core modules exist and are importable."""
        import importlib
        modules = [
            "learning_store", "self_evolution", "tool_synthesizer",
            "difficulty_router", "agent_factory", "dag_engine",
            "hybrid_retrieval", "input_guard", "orchestrator", "feedback_loop",
        ]
        project_root = Path(r"D:\Prospects\ScreenMemory")
        health: Dict[str, Any] = {}

        for mod_name in modules:
            entry: Dict[str, Any] = {"exists": False, "importable": False, "loc": 0}
            mod_path = project_root / "core" / f"{mod_name}.py"
            try:
                if mod_path.exists():
                    entry["exists"] = True
                    lines = mod_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                    entry["loc"] = len([l for l in lines if l.strip() and not l.strip().startswith("#")])
            except OSError:
                pass

            try:
                importlib.import_module(f"core.{mod_name}")
                entry["importable"] = True
            except Exception:
                try:
                    # Fallback: try importing from file directly
                    spec = importlib.util.spec_from_file_location(mod_name, str(mod_path))
                    if spec and spec.loader:
                        entry["importable"] = True
                except Exception:
                    pass

            health[mod_name] = entry
        return health

    def get_full_state(self) -> Dict[str, Any]:
        """Combine ALL awareness methods into one comprehensive state dict."""
        state: Dict[str, Any] = {"timestamp": datetime.now().isoformat()}
        methods = {
            "processes": self.get_process_map,
            "agents": self.get_live_agent_status,
            "architecture": self.get_architecture_health,
            "resources": self.get_resource_usage,
            "file_changes": self.get_file_changes,
            "errors": self.get_recent_errors,
            "tasks": self.get_task_history,
        }
        for key, method in methods.items():
            try:
                state[key] = method()
            except Exception as exc:
                state[key] = {"_error": str(exc)}
        return state

    def detect_anomalies(self) -> List[str]:
        """Analyze system state and return warnings."""
        warnings: List[str] = []
        try:
            # --- Duplicate processes ---
            proc_map = self.get_process_map()
            from collections import Counter
            proc_names = [k.split("_")[0] if not k.startswith("pid_") else k for k in proc_map]
            counts = Counter(proc_names)
            for name, count in counts.items():
                if count > 1 and name != "_error":
                    warnings.append(f"Duplicate processes: {count} instances of {name}")

            # --- Idle agents with pending tasks ---
            agent_status = self.get_live_agent_status()
            now = time.time()
            for agent, data in agent_status.get("agents", {}).items():
                status = data.get("status", "unknown")
                updated = data.get("last_updated") or data.get("updated_at") or data.get("timestamp")
                pending = agent_status.get("pending_queues", {}).get(agent, {}).get("count", 0)
                if status.upper() in ("IDLE", "WAITING") and pending > 0:
                    idle_minutes = 0
                    if updated:
                        try:
                            ts = datetime.fromisoformat(str(updated)).timestamp()
                            idle_minutes = round((now - ts) / 60, 1)
                        except (ValueError, TypeError, OSError):
                            pass
                    if idle_minutes > 5:
                        warnings.append(
                            f"Agent {agent} has been IDLE for >{idle_minutes:.0f} minutes with {pending} pending tasks"
                        )

            # --- Memory usage ---
            try:
                import psutil
                mem = psutil.virtual_memory()
                if mem.percent > 80:
                    warnings.append(f"Memory usage above 80% (currently {mem.percent}%)")
            except ImportError:
                pass

            # --- No recent results ---
            results = agent_status.get("results", {})
            if not results:
                # Check if any result files exist at all
                result_files = list(AGENT_QUEUE_DIR.glob("*_result.json")) if AGENT_QUEUE_DIR.exists() else []
                if not result_files:
                    task_hist = self.get_task_history(limit=1)
                    if task_hist.get("total", 0) == 0:
                        warnings.append("No agent results in last 10 minutes — pipeline may be stalled")
            else:
                any_recent = False
                for agent, rdata in results.items():
                    latest = rdata.get("latest") or {}
                    ts = latest.get("timestamp") or latest.get("completed_at")
                    if ts:
                        try:
                            t = datetime.fromisoformat(str(ts)).timestamp()
                            if now - t < 600:
                                any_recent = True
                                break
                        except (ValueError, TypeError):
                            pass
                if not any_recent:
                    warnings.append("No agent results in last 10 minutes — pipeline may be stalled")

            # --- Stale orchestrator thinking ---
            orch_file = AGENT_QUEUE_DIR / "orch_thinking.json"
            if orch_file.exists():
                try:
                    age = now - orch_file.stat().st_mtime
                    if age > 300:
                        warnings.append(f"Orchestrator thinking file stale (>{int(age // 60)} min old)")
                except OSError:
                    pass

        except Exception as exc:
            warnings.append(f"Anomaly detection error: {exc}")
        return warnings

    def format_god_briefing(self) -> str:
        """Produce a human-readable status summary for GOD."""
        try:
            agents_data = self.get_live_agent_status()
            resources = self.get_resource_usage()
            arch = self.get_architecture_health()
            anomalies = self.detect_anomalies()
            tasks = self.get_task_history(limit=1000)

            # --- SYSTEM line ---
            agent_count = len(agents_data.get("agents", {}))
            pending_approvals = len(agents_data.get("god_queue", []))
            tasks_done = tasks.get("total", 0)
            system_line = f"SYSTEM: {agent_count} agents | {pending_approvals} pending approvals | {tasks_done} tasks completed"

            # --- HEALTH line ---
            cpu = resources.get("cpu_percent", "?")
            mem_info = resources.get("memory", {})
            mem_used = mem_info.get("used_gb", "?")
            mem_total = mem_info.get("total_gb", "?")
            disk_pct = resources.get("disk", {}).get("percent", "?")
            health_line = f"HEALTH: CPU {cpu}% | RAM {mem_used}/{mem_total} GB | Disk {disk_pct}%"

            # --- AGENTS line ---
            agent_parts = []
            for name, data in agents_data.get("agents", {}).items():
                st = data.get("status", "UNKNOWN").upper()
                agent_parts.append(f"{name}={st}")
            agents_line = "AGENTS: " + (" ".join(agent_parts) if agent_parts else "none detected")

            # --- ALERTS ---
            if anomalies:
                alert_lines = "\n".join(f"  ⚠ {a}" for a in anomalies)
            else:
                alert_lines = "  ✓ No anomalies detected"

            # --- ARCHITECTURE ---
            healthy = sum(1 for v in arch.values() if v.get("exists"))
            total = len(arch)
            arch_line = f"ARCHITECTURE: {healthy}/{total} modules healthy"

            briefing = (
                f"\n═══ GOD BRIEFING ═══\n"
                f"{system_line}\n"
                f"{health_line}\n"
                f"{agents_line}\n"
                f"ALERTS:\n{alert_lines}\n"
                f"{arch_line}\n"
                f"═══════════════════\n"
            )
            return briefing
        except Exception as exc:
            return f"\n═══ GOD BRIEFING ═══\nError generating briefing: {exc}\n═══════════════════\n"


# ============================================================================
# GOD CONSOLE — The Authority Layer
# ============================================================================

class GodConsole:
    """
    The authority layer. Queues decisions that need human approval.

    GOD is the human operator. Every critical, destructive, or external
    action must pass through this console before execution.

    Usage:
        god = GodConsole()
        approval_id = god.add_approval("deploy_prod", "alpha", RiskLevel.CRITICAL, "Deploy v2.1")
        god.approve(approval_id)                      # GOD says yes
        god.set_directive("Increase test coverage", 2) # GOD sets a goal
        state = god.get_system_state()                 # GOD sees everything
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(DB_PATH)
        self.lock = threading.Lock()
        self.awareness = SystemAwareness(self.db_path)

        # Ensure directories exist
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)

        self._init_db()
        logger.info("GOD Console initialized — authority layer active")

    # ------------------------------------------------------------------
    # DATABASE INIT
    # ------------------------------------------------------------------

    def _init_db(self):
        """Initialize all GOD console tables."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    detail TEXT,
                    timestamp REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    reviewed_at REAL,
                    rejection_reason TEXT,
                    auto_expire_seconds INTEGER DEFAULT 3600
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_approvals_status
                ON approvals(status, timestamp)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS directives (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    sub_tasks TEXT DEFAULT '[]',
                    completed_at REAL,
                    notes TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS overrides (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    metadata TEXT DEFAULT '{}'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS system_errors (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT,
                    error_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    traceback TEXT,
                    timestamp REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_log (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT,
                    task_type TEXT NOT NULL,
                    description TEXT,
                    success INTEGER NOT NULL,
                    started_at REAL,
                    completed_at REAL NOT NULL,
                    duration_ms REAL,
                    error TEXT
                )
            """)
            conn.commit()

    # ------------------------------------------------------------------
    # APPROVAL QUEUE
    # ------------------------------------------------------------------

    def add_approval(
        self,
        action: str,
        agent_id: str,
        risk_level: Optional[RiskLevel] = None,
        detail: str = "",
        auto_expire_seconds: int = 3600,
    ) -> str:
        """
        Queue an action for GOD approval. Returns the approval ID.

        If risk_level is not provided, it is auto-classified from the action string.
        """
        if risk_level is None:
            risk_level = classify_risk(action)

        approval_id = f"approval_{uuid4().hex[:12]}"
        approval = PendingApproval(
            id=approval_id,
            action=action,
            agent_id=agent_id,
            risk_level=risk_level.value if isinstance(risk_level, RiskLevel) else risk_level,
            detail=detail,
            timestamp=time.time(),
            auto_expire_seconds=auto_expire_seconds,
        )

        with self.lock:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                d = approval.to_dict()
                conn.execute("""
                    INSERT INTO approvals
                    (id, action, agent_id, risk_level, detail, timestamp, status,
                     reviewed_at, rejection_reason, auto_expire_seconds)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    d["id"], d["action"], d["agent_id"], d["risk_level"],
                    d["detail"], d["timestamp"], d["status"],
                    d["reviewed_at"], d["rejection_reason"], d["auto_expire_seconds"],
                ))
                conn.commit()

        self._sync_queue_file()
        logger.info(
            f"Approval queued: {approval_id} | {action} | "
            f"agent={agent_id} | risk={approval.risk_level}"
        )
        return approval_id

    def approve(self, approval_id: str) -> bool:
        """GOD approves the action. Returns True if found and approved."""
        return self._resolve_approval(approval_id, ApprovalStatus.APPROVED)

    def reject(self, approval_id: str, reason: str = "") -> bool:
        """GOD rejects the action. Returns True if found and rejected."""
        return self._resolve_approval(approval_id, ApprovalStatus.REJECTED, reason)

    def _resolve_approval(
        self, approval_id: str, status: ApprovalStatus, reason: str = ""
    ) -> bool:
        with self.lock:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                cursor = conn.execute(
                    "SELECT status FROM approvals WHERE id = ?", (approval_id,)
                )
                row = cursor.fetchone()
                if not row or row[0] != ApprovalStatus.PENDING.value:
                    return False

                conn.execute("""
                    UPDATE approvals
                    SET status = ?, reviewed_at = ?, rejection_reason = ?
                    WHERE id = ?
                """, (status.value, time.time(), reason or None, approval_id))
                conn.commit()

        self._sync_queue_file()
        logger.info(f"Approval {approval_id} → {status.value}" +
                     (f" (reason: {reason})" if reason else ""))
        return True

    def get_pending(self) -> List[PendingApproval]:
        """Get all pending approvals, expiring stale ones first."""
        self._expire_stale()
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM approvals
                WHERE status = 'pending'
                ORDER BY
                    CASE risk_level
                        WHEN 'critical' THEN 0
                        WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2
                        WHEN 'low' THEN 3
                    END,
                    timestamp ASC
            """)
            return [PendingApproval.from_row(row) for row in cursor]

    def get_approval(self, approval_id: str) -> Optional[PendingApproval]:
        """Get a single approval by ID."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            )
            row = cursor.fetchone()
            return PendingApproval.from_row(row) if row else None

    def get_approval_history(self, limit: int = 50) -> List[PendingApproval]:
        """Get recently resolved approvals."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM approvals
                WHERE status != 'pending'
                ORDER BY reviewed_at DESC LIMIT ?
            """, (limit,))
            return [PendingApproval.from_row(row) for row in cursor]

    def _expire_stale(self):
        """Mark expired approvals."""
        now = time.time()
        with self.lock:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                conn.execute("""
                    UPDATE approvals
                    SET status = 'expired', reviewed_at = ?
                    WHERE status = 'pending'
                    AND (? - timestamp) > auto_expire_seconds
                """, (now, now))
                if conn.total_changes > 0:
                    conn.commit()
                    self._sync_queue_file()

    # ------------------------------------------------------------------
    # DIRECTIVES
    # ------------------------------------------------------------------

    def set_directive(
        self,
        goal_text: str,
        priority: int = 5,
        notes: str = "",
    ) -> str:
        """Add a high-level goal for the orchestrator. Returns directive ID."""
        directive_id = f"dir_{uuid4().hex[:12]}"
        directive = GodDirective(
            id=directive_id,
            goal=goal_text,
            priority=max(1, min(10, priority)),
            created_at=time.time(),
            notes=notes,
        )

        with self.lock:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                d = directive.to_dict()
                conn.execute("""
                    INSERT INTO directives
                    (id, goal, priority, created_at, status, sub_tasks, completed_at, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    d["id"], d["goal"], d["priority"], d["created_at"],
                    d["status"], d["sub_tasks"], d["completed_at"], d["notes"],
                ))
                conn.commit()

        self._sync_queue_file()
        logger.info(f"Directive set: {directive_id} | P{priority} | {goal_text}")
        return directive_id

    def complete_directive(self, directive_id: str) -> bool:
        """Mark a directive as completed."""
        return self._update_directive_status(directive_id, DirectiveStatus.COMPLETED)

    def cancel_directive(self, directive_id: str) -> bool:
        """Cancel a directive."""
        return self._update_directive_status(directive_id, DirectiveStatus.CANCELLED)

    def _update_directive_status(self, directive_id: str, status: DirectiveStatus) -> bool:
        with self.lock:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                cursor = conn.execute(
                    "SELECT status FROM directives WHERE id = ?", (directive_id,)
                )
                row = cursor.fetchone()
                if not row or row[0] != DirectiveStatus.ACTIVE.value:
                    return False

                conn.execute("""
                    UPDATE directives SET status = ?, completed_at = ?
                    WHERE id = ?
                """, (status.value, time.time(), directive_id))
                conn.commit()

        self._sync_queue_file()
        logger.info(f"Directive {directive_id} → {status.value}")
        return True

    def add_sub_task(self, directive_id: str, sub_task: str) -> bool:
        """Append a sub-task to a directive (orchestrator calls this)."""
        with self.lock:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                cursor = conn.execute(
                    "SELECT sub_tasks FROM directives WHERE id = ?", (directive_id,)
                )
                row = cursor.fetchone()
                if not row:
                    return False

                tasks = json.loads(row[0]) if row[0] else []
                tasks.append(sub_task)
                conn.execute(
                    "UPDATE directives SET sub_tasks = ? WHERE id = ?",
                    (json.dumps(tasks), directive_id),
                )
                conn.commit()
        return True

    def get_active_directives(self) -> List[GodDirective]:
        """Get all active directives ordered by priority."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM directives
                WHERE status = 'active'
                ORDER BY priority ASC, created_at ASC
            """)
            return [GodDirective.from_dict(dict(row)) for row in cursor]

    def get_all_directives(self, limit: int = 50) -> List[GodDirective]:
        """Get all directives regardless of status."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM directives
                ORDER BY created_at DESC LIMIT ?
            """, (limit,))
            return [GodDirective.from_dict(dict(row)) for row in cursor]

    # ------------------------------------------------------------------
    # OVERRIDES — halt, resume, reprioritize agents
    # ------------------------------------------------------------------

    def override_agent(
        self,
        agent_id: str,
        action: AgentAction,
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Override an agent's state. Writes to the agent's live queue file
        and records the override in the database.
        """
        override_id = f"ovr_{uuid4().hex[:12]}"
        override = AgentOverride(
            id=override_id,
            agent_id=agent_id,
            action=action.value if isinstance(action, AgentAction) else action,
            reason=reason,
            timestamp=time.time(),
            metadata=metadata or {},
        )

        # Persist override record
        with self.lock:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                d = override.to_dict()
                conn.execute("""
                    INSERT INTO overrides
                    (id, agent_id, action, reason, timestamp, metadata)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (d["id"], d["agent_id"], d["action"],
                      d["reason"], d["timestamp"], d["metadata"]))
                conn.commit()

        # Write override signal to the agent's live file
        self._signal_agent(agent_id, action, reason)

        self._sync_queue_file()
        logger.info(f"Override: {agent_id} → {action} | {reason}")
        return override_id

    def _signal_agent(
        self,
        agent_id: str,
        action: AgentAction,
        reason: str,
    ):
        """Write an override signal to the agent's live queue file."""
        live_file = AGENT_QUEUE_DIR / f"{agent_id}_live.json"
        try:
            if live_file.exists():
                data = json.loads(live_file.read_text(encoding="utf-8"))
            else:
                data = {}

            action_val = action.value if isinstance(action, AgentAction) else action
            if action_val == AgentAction.HALT.value:
                data["status"] = "HALTED_BY_GOD"
            elif action_val == AgentAction.RESUME.value:
                data["status"] = "WORKING"
            elif action_val == AgentAction.RESTART.value:
                data["status"] = "RESTART_REQUESTED"

            data["god_override"] = {
                "action": action_val,
                "reason": reason,
                "timestamp": time.time(),
            }

            live_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as e:
            logger.error(f"Failed to signal agent {agent_id}: {e}")

    def get_override_history(self, agent_id: Optional[str] = None, limit: int = 50) -> List[Dict]:
        """Get override history, optionally filtered by agent."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            if agent_id:
                cursor = conn.execute("""
                    SELECT * FROM overrides
                    WHERE agent_id = ?
                    ORDER BY timestamp DESC LIMIT ?
                """, (agent_id, limit))
            else:
                cursor = conn.execute("""
                    SELECT * FROM overrides
                    ORDER BY timestamp DESC LIMIT ?
                """, (limit,))
            return [dict(row) for row in cursor]

    # ------------------------------------------------------------------
    # SYSTEM STATE — Everything GOD sees
    # ------------------------------------------------------------------

    def get_system_state(self) -> Dict[str, Any]:
        """
        Full dashboard state for GOD. Returns a single dict with:
        agents, pending_approvals, active_directives, recent_errors,
        task_stats, resources, uptime, file_changes.
        """
        pending = self.get_pending()
        directives = self.get_active_directives()
        agents = self.awareness.get_agent_health()
        errors = self.awareness.get_recent_errors(limit=20)
        tasks = self.awareness.get_task_history(limit=50)
        resources = self.awareness.get_resource_usage()
        file_changes = self.awareness.get_file_changes(hours=24)

        return {
            "timestamp": datetime.now().isoformat(),
            "uptime_seconds": resources.get("uptime_seconds", 0),
            "agents": agents,
            "pending_approvals": [a.to_dict() for a in pending],
            "pending_count": len(pending),
            "active_directives": [d.to_dict() for d in directives],
            "recent_errors": errors,
            "task_stats": {
                "total": tasks["total"],
                "success": tasks["success"],
                "failed": tasks["failed"],
                "success_rate": (
                    round(tasks["success"] / tasks["total"], 3)
                    if tasks["total"] > 0 else 0.0
                ),
            },
            "resources": resources,
            "recent_file_changes": file_changes[:20],
        }

    # ------------------------------------------------------------------
    # ERROR & TASK LOGGING — for agents to report into
    # ------------------------------------------------------------------

    def log_error(
        self,
        error_type: str,
        message: str,
        agent_id: str = "",
        traceback_str: str = "",
    ):
        """Log a system error for GOD visibility."""
        error_id = f"err_{uuid4().hex[:12]}"
        with self.lock:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                conn.execute("""
                    INSERT INTO system_errors
                    (id, agent_id, error_type, message, traceback, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (error_id, agent_id, error_type, message,
                      traceback_str, time.time()))
                conn.commit()

    def log_task(
        self,
        task_type: str,
        success: bool,
        agent_id: str = "",
        description: str = "",
        duration_ms: float = 0.0,
        error: str = "",
    ):
        """Log a completed task for GOD visibility."""
        task_id = f"task_{uuid4().hex[:12]}"
        with self.lock:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                conn.execute("""
                    INSERT INTO task_log
                    (id, agent_id, task_type, description, success,
                     started_at, completed_at, duration_ms, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    task_id, agent_id, task_type, description,
                    int(success), time.time() - (duration_ms / 1000),
                    time.time(), duration_ms, error or None,
                ))
                conn.commit()

    # ------------------------------------------------------------------
    # QUEUE FILE SYNC — Dashboard polling
    # ------------------------------------------------------------------

    def _sync_queue_file(self):
        """Write current state to god_queue.json so the dashboard can poll it."""
        try:
            pending = self.get_pending()
            directives = self.get_active_directives()

            queue_data = {
                "updated_at": datetime.now().isoformat(),
                "pending_approvals": [a.to_dict() for a in pending],
                "pending_count": len(pending),
                "active_directives": [d.to_dict() for d in directives],
                "critical_count": sum(
                    1 for a in pending if a.risk_level == RiskLevel.CRITICAL.value
                ),
                "high_count": sum(
                    1 for a in pending if a.risk_level == RiskLevel.HIGH.value
                ),
            }

            QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
            QUEUE_PATH.write_text(
                json.dumps(queue_data, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"Failed to sync god_queue.json: {e}")

    # ------------------------------------------------------------------
    # CONVENIENCE — risk gate for agents
    # ------------------------------------------------------------------

    def requires_approval(self, action: str) -> bool:
        """Check whether an action needs GOD approval based on risk level."""
        level = classify_risk(action)
        return level in (RiskLevel.CRITICAL, RiskLevel.HIGH)

    def gate(
        self,
        action: str,
        agent_id: str,
        detail: str = "",
    ) -> Tuple[bool, Optional[str]]:
        """
        Risk gate for agents. If the action is low/medium risk, returns
        (True, None) — proceed. If high/critical, queues an approval and
        returns (False, approval_id) — agent must wait.
        """
        level = classify_risk(action)
        if level in (RiskLevel.LOW, RiskLevel.MEDIUM):
            return True, None

        approval_id = self.add_approval(action, agent_id, level, detail)
        return False, approval_id

    def is_approved(self, approval_id: str) -> Optional[bool]:
        """
        Check if an approval has been resolved.
        Returns True (approved), False (rejected/expired), or None (still pending).
        """
        approval = self.get_approval(approval_id)
        if not approval:
            return False
        if approval.status == ApprovalStatus.APPROVED.value:
            return True
        if approval.status in (ApprovalStatus.REJECTED.value, ApprovalStatus.EXPIRED.value):
            return False
        return None  # still pending
