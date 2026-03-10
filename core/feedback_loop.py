"""
Feedback Loop — Learning from task outcomes to improve agent routing.

Records every task outcome, tracks per-agent and per-task-type statistics,
and uses accumulated data to suggest better routing decisions and
surface systemic issues.

Wired into AutoOrchestrator.check_results() so every completed task
feeds back into the loop automatically.
"""

import sqlite3
import json
import time
import threading
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger("feedback_loop")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
QUEUE_DIR = DATA_DIR / "agent_queues"
FEEDBACK_DB = DATA_DIR / "feedback.db"
FEEDBACK_JSON = QUEUE_DIR / "feedback.json"


@dataclass
class TaskOutcome:
    """Records what happened after a task ran."""
    task_id: str
    agent_id: str
    task_type: str
    description: str
    success: bool
    duration_ms: float
    error: Optional[str]
    output_summary: str
    timestamp: float


class FeedbackLoop:
    """
    Persistent feedback store that tracks task outcomes, agent performance,
    and task-type patterns to enable data-driven routing decisions.
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or str(FEEDBACK_DB)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------
    # DB setup
    # ------------------------------------------------------------------

    def _init_db(self):
        """Create tables if they don't exist."""
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS outcomes (
                    task_id      TEXT PRIMARY KEY,
                    agent_id     TEXT NOT NULL,
                    task_type    TEXT NOT NULL,
                    description  TEXT,
                    success      INTEGER NOT NULL,
                    duration_ms  REAL NOT NULL,
                    error        TEXT,
                    output_summary TEXT,
                    timestamp    REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_stats (
                    agent_id       TEXT PRIMARY KEY,
                    total_tasks    INTEGER NOT NULL DEFAULT 0,
                    success_count  INTEGER NOT NULL DEFAULT 0,
                    fail_count     INTEGER NOT NULL DEFAULT 0,
                    avg_duration_ms REAL NOT NULL DEFAULT 0,
                    last_updated   REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_patterns (
                    task_type      TEXT NOT NULL,
                    agent_id       TEXT NOT NULL,
                    success_rate   REAL NOT NULL DEFAULT 0,
                    avg_duration   REAL NOT NULL DEFAULT 0,
                    sample_count   INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (task_type, agent_id)
                );

                CREATE INDEX IF NOT EXISTS idx_outcomes_agent
                    ON outcomes(agent_id);
                CREATE INDEX IF NOT EXISTS idx_outcomes_type
                    ON outcomes(task_type);
                CREATE INDEX IF NOT EXISTS idx_outcomes_ts
                    ON outcomes(timestamp);
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ------------------------------------------------------------------
    # Record outcome
    # ------------------------------------------------------------------

    def record_outcome(self, outcome: TaskOutcome):
        """Insert outcome, update agent_stats and task_patterns."""
        with self._lock:
            with self._connect() as conn:
                # 1. Insert outcome
                conn.execute(
                    """INSERT OR REPLACE INTO outcomes
                       (task_id, agent_id, task_type, description, success,
                        duration_ms, error, output_summary, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (outcome.task_id, outcome.agent_id, outcome.task_type,
                     outcome.description, int(outcome.success),
                     outcome.duration_ms, outcome.error,
                     outcome.output_summary, outcome.timestamp),
                )

                # 2. Upsert agent_stats
                conn.execute("""
                    INSERT INTO agent_stats (agent_id, total_tasks, success_count,
                                            fail_count, avg_duration_ms, last_updated)
                    VALUES (?, 1, ?, ?, ?, ?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                        total_tasks   = total_tasks + 1,
                        success_count = success_count + ?,
                        fail_count    = fail_count + ?,
                        avg_duration_ms = (avg_duration_ms * total_tasks + ?) / (total_tasks + 1),
                        last_updated  = ?
                """, (
                    outcome.agent_id,
                    int(outcome.success), int(not outcome.success),
                    outcome.duration_ms, outcome.timestamp,
                    int(outcome.success), int(not outcome.success),
                    outcome.duration_ms, outcome.timestamp,
                ))

                # 3. Upsert task_patterns
                row = conn.execute(
                    """SELECT success_rate, avg_duration, sample_count
                       FROM task_patterns WHERE task_type = ? AND agent_id = ?""",
                    (outcome.task_type, outcome.agent_id),
                ).fetchone()

                if row:
                    n = row["sample_count"]
                    new_n = n + 1
                    new_rate = (row["success_rate"] * n + int(outcome.success)) / new_n
                    new_dur = (row["avg_duration"] * n + outcome.duration_ms) / new_n
                    conn.execute(
                        """UPDATE task_patterns
                           SET success_rate = ?, avg_duration = ?, sample_count = ?
                           WHERE task_type = ? AND agent_id = ?""",
                        (new_rate, new_dur, new_n,
                         outcome.task_type, outcome.agent_id),
                    )
                else:
                    conn.execute(
                        """INSERT INTO task_patterns
                           (task_type, agent_id, success_rate, avg_duration, sample_count)
                           VALUES (?, ?, ?, ?, 1)""",
                        (outcome.task_type, outcome.agent_id,
                         float(outcome.success), outcome.duration_ms),
                    )

        # 4. Write to dashboard JSON
        self._write_dashboard_json(outcome)

    def _write_dashboard_json(self, outcome: TaskOutcome):
        """Append latest outcome to feedback.json for dashboard visibility."""
        try:
            entries: List[Dict] = []
            if FEEDBACK_JSON.exists():
                with open(FEEDBACK_JSON, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    entries = data[-99:]  # keep last 100
            entries.append({
                "task_id": outcome.task_id,
                "agent": outcome.agent_id,
                "type": outcome.task_type,
                "success": outcome.success,
                "duration_ms": outcome.duration_ms,
                "error": outcome.error,
                "time": datetime.fromtimestamp(outcome.timestamp).strftime("%H:%M:%S"),
            })
            with open(FEEDBACK_JSON, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=1)
        except (OSError, json.JSONDecodeError) as e:
            logger.debug(f"Dashboard JSON write error: {e}")

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def get_best_agent(self, task_type: str, min_samples: int = 3) -> Optional[str]:
        """Return agent_id with highest success_rate for task_type (min N samples)."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT agent_id FROM task_patterns
                   WHERE task_type = ? AND sample_count >= ?
                   ORDER BY success_rate DESC, avg_duration ASC
                   LIMIT 1""",
                (task_type, min_samples),
            ).fetchone()
        return row["agent_id"] if row else None

    def get_agent_report(self, agent_id: str) -> dict:
        """Return stats dict for a single agent."""
        with self._connect() as conn:
            stats = conn.execute(
                "SELECT * FROM agent_stats WHERE agent_id = ?", (agent_id,)
            ).fetchone()

            if not stats:
                return {"agent_id": agent_id, "total": 0, "success_pct": 0,
                        "fail_pct": 0, "avg_duration_ms": 0, "recent_errors": []}

            total = stats["total_tasks"]
            success_pct = round(stats["success_count"] / total * 100, 1) if total else 0
            fail_pct = round(stats["fail_count"] / total * 100, 1) if total else 0

            errors = conn.execute(
                """SELECT task_id, error, timestamp FROM outcomes
                   WHERE agent_id = ? AND success = 0 AND error IS NOT NULL
                   ORDER BY timestamp DESC LIMIT 5""",
                (agent_id,),
            ).fetchall()

        return {
            "agent_id": agent_id,
            "total": total,
            "success_pct": success_pct,
            "fail_pct": fail_pct,
            "avg_duration_ms": round(stats["avg_duration_ms"], 1),
            "recent_errors": [
                {"task_id": e["task_id"], "error": e["error"],
                 "time": datetime.fromtimestamp(e["timestamp"]).strftime("%H:%M:%S")}
                for e in errors
            ],
        }

    def get_system_report(self, recent_failures: int = 10) -> dict:
        """Aggregate report across all agents."""
        with self._connect() as conn:
            agg = conn.execute(
                """SELECT COUNT(*) as total,
                          SUM(success) as successes,
                          AVG(duration_ms) as avg_dur
                   FROM outcomes"""
            ).fetchone()

            total = agg["total"] or 0
            successes = agg["successes"] or 0
            success_rate = round(successes / total * 100, 1) if total else 0

            busiest = conn.execute(
                """SELECT agent_id, COUNT(*) as cnt FROM outcomes
                   GROUP BY agent_id ORDER BY cnt DESC LIMIT 1"""
            ).fetchone()

            error_prone = conn.execute(
                """SELECT agent_id,
                          CAST(SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS REAL)
                              / COUNT(*) as fail_rate
                   FROM outcomes GROUP BY agent_id
                   HAVING COUNT(*) >= 2
                   ORDER BY fail_rate DESC LIMIT 1"""
            ).fetchone()

            failures = conn.execute(
                """SELECT task_id, agent_id, task_type, error, timestamp
                   FROM outcomes WHERE success = 0
                   ORDER BY timestamp DESC LIMIT ?""",
                (recent_failures,),
            ).fetchall()

        return {
            "total_tasks": total,
            "success_rate": success_rate,
            "avg_duration_ms": round(agg["avg_dur"] or 0, 1),
            "busiest_agent": busiest["agent_id"] if busiest else None,
            "most_error_prone_agent": error_prone["agent_id"] if error_prone else None,
            "recent_failures": [
                {"task_id": f["task_id"], "agent": f["agent_id"],
                 "type": f["task_type"], "error": f["error"],
                 "time": datetime.fromtimestamp(f["timestamp"]).strftime("%H:%M:%S")}
                for f in failures
            ],
        }

    # ------------------------------------------------------------------
    # Improvement suggestions
    # ------------------------------------------------------------------

    def suggest_improvements(self) -> List[str]:
        """Analyze patterns and return actionable suggestions."""
        suggestions: List[str] = []

        with self._connect() as conn:
            # 1. Agents with high failure rates on specific task types
            patterns = conn.execute(
                """SELECT tp.task_type, tp.agent_id, tp.success_rate, tp.sample_count
                   FROM task_patterns tp
                   WHERE tp.sample_count >= 2 AND tp.success_rate < 0.6
                   ORDER BY tp.success_rate ASC"""
            ).fetchall()

            for p in patterns:
                fail_pct = round((1 - p["success_rate"]) * 100)
                # Find a better agent for this task type
                better = conn.execute(
                    """SELECT agent_id, success_rate FROM task_patterns
                       WHERE task_type = ? AND agent_id != ? AND sample_count >= 2
                       ORDER BY success_rate DESC LIMIT 1""",
                    (p["task_type"], p["agent_id"]),
                ).fetchone()
                if better and better["success_rate"] > p["success_rate"]:
                    suggestions.append(
                        f"Agent {p['agent_id']} has {fail_pct}% failure rate on "
                        f"'{p['task_type']}' tasks — consider reassigning to "
                        f"Agent {better['agent_id']}"
                    )
                else:
                    suggestions.append(
                        f"Agent {p['agent_id']} has {fail_pct}% failure rate on "
                        f"'{p['task_type']}' tasks — investigate root cause"
                    )

            # 2. Slow task types
            slow_types = conn.execute(
                """SELECT task_type, AVG(duration_ms) as avg_dur, COUNT(*) as cnt
                   FROM outcomes GROUP BY task_type
                   HAVING cnt >= 2 AND avg_dur > 30000
                   ORDER BY avg_dur DESC"""
            ).fetchall()
            for s in slow_types:
                avg_sec = round(s["avg_dur"] / 1000)
                suggestions.append(
                    f"Task type '{s['task_type']}' averages {avg_sec}s "
                    f"— check for bottlenecks"
                )

            # 3. Idle agents (no tasks in last 2 hours)
            cutoff = time.time() - 7200
            all_agents = conn.execute(
                "SELECT agent_id, last_updated FROM agent_stats"
            ).fetchall()
            for a in all_agents:
                if a["last_updated"] < cutoff:
                    hours_idle = round((time.time() - a["last_updated"]) / 3600, 1)
                    suggestions.append(
                        f"Agent {a['agent_id']} has been idle for "
                        f"{hours_idle} hours — consider rebalancing"
                    )

            # 4. Overall low success rate warning
            overall = conn.execute(
                """SELECT COUNT(*) as total, SUM(success) as ok FROM outcomes"""
            ).fetchone()
            if overall["total"] and overall["total"] >= 5:
                rate = overall["ok"] / overall["total"]
                if rate < 0.7:
                    suggestions.append(
                        f"Overall system success rate is {round(rate*100)}% "
                        f"— review agent configurations and task decomposition"
                    )

        return suggestions
