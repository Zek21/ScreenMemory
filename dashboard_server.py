"""
Dashboard Server v2 — Skynet Control Center
Serves live agent board with Skynet console, orchestrator brain feed, and per-agent monitoring.
Polls agent status files at high frequency for real-time feel.
"""
import os
import sys
import json
import time
import threading
import socketserver
from http.server import SimpleHTTPRequestHandler, HTTPServer
from pathlib import Path
from datetime import datetime
import logging

logger = logging.getLogger("skynet.dashboard")

QUEUE_DIR = Path(r"D:\Prospects\ScreenMemory\data\agent_queues")
DASHBOARD_DIR = Path(r"D:\Prospects\ScreenMemory")
TASK_QUEUE_FILE = QUEUE_DIR / "task_queue.json"
PROJECT_ROOT = DASHBOARD_DIR

# --- GOD awareness & feedback (lazy singletons, cached) ---
sys.path.insert(0, str(DASHBOARD_DIR))

_awareness = None
_feedback = None
_god_state_cache = None
_god_state_cache_time = 0
GOD_STATE_TTL = 5  # seconds

def _get_awareness():
    global _awareness
    if _awareness is None:
        from core.god_console import SystemAwareness
        _awareness = SystemAwareness()
    return _awareness

def _get_feedback():
    global _feedback
    if _feedback is None:
        from core.feedback_loop import FeedbackLoop
        _feedback = FeedbackLoop()
    return _feedback

def _build_god_state() -> dict:
    """Build comprehensive GOD state, cached for GOD_STATE_TTL seconds."""
    global _god_state_cache, _god_state_cache_time
    now = time.time()
    if _god_state_cache and (now - _god_state_cache_time) < GOD_STATE_TTL:
        return _god_state_cache

    awareness = _get_awareness()
    feedback = _get_feedback()

    try:
        briefing = awareness.format_god_briefing()
    except Exception as e:
        briefing = f"Error: {e}"

    try:
        anomalies = awareness.detect_anomalies()
    except Exception as e:
        anomalies = [f"Error detecting anomalies: {e}"]

    try:
        fb_report = feedback.get_system_report()
    except Exception as e:
        fb_report = {"error": str(e)}

    try:
        full = awareness.get_full_state()
        architecture = full.get("architecture", {})
        processes = full.get("processes", {})
    except Exception as e:
        architecture = {"error": str(e)}
        processes = {"error": str(e)}

    _god_state_cache = {
        "briefing": briefing,
        "anomalies": anomalies,
        "feedback": fb_report,
        "architecture": architecture,
        "processes": processes,
        "cached_at": now,
    }
    _god_state_cache_time = now
    return _god_state_cache

# Shared state
agent_states = {
    "alpha": {"status": "IDLE", "tasks_completed": 0, "current_task": None, "recent_logs": [], "progress": 0},
    "beta":  {"status": "IDLE", "tasks_completed": 0, "current_task": None, "recent_logs": [], "progress": 0},
    "gamma": {"status": "IDLE", "tasks_completed": 0, "current_task": None, "recent_logs": [], "progress": 0},
    "delta": {"status": "IDLE", "tasks_completed": 0, "current_task": None, "recent_logs": [], "progress": 0},
}
orch_thinking = []  # orchestrator brain feed
god_queue = []      # GOD console items needing authority
log_counter = 0
orch_counter = 0
god_counter = 0
lock = threading.RLock()


class ThreadedServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def _send_json(self, data):
        """Send a JSON response with CORS headers."""
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        origin = self.headers.get('Origin', '')
        allowed = origin if origin.startswith(('http://localhost', 'http://127.0.0.1')) else 'http://localhost'
        self.send_header('Access-Control-Allow-Origin', allowed)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/' or self.path.startswith('/?'):
            self.path = '/dashboard.html'
            # No-cache headers to prevent stale content
            return super().do_GET()
        elif self.path == '/status':
            # Read bus messages for display
            bus_msgs = []
            bus_file = QUEUE_DIR / "message_bus.json"
            if bus_file.exists():
                try:
                    bus_data = json.loads(bus_file.read_text(encoding="utf-8"))
                    bus_msgs = bus_data[-20:] if isinstance(bus_data, list) else []
                except (json.JSONDecodeError, OSError) as e:
                    logger.debug(f"Failed to read message bus: {e}")
            with lock:
                payload = {
                    "agents": {k: dict(v) for k, v in agent_states.items()},
                    "orch_thinking": list(orch_thinking[-100:]),
                    "god_queue": list(god_queue),
                    "bus": bus_msgs,
                }
            self._send_json(payload)
        elif self.path == '/god_state':
            try:
                payload = _build_god_state()
            except Exception as exc:
                payload = {"error": str(exc)}
            self._send_json(payload)
        elif self.path == '/brain':
            try:
                sys.path.insert(0, str(PROJECT_ROOT))
                from brain_bridge import BrainBridge
                brain = BrainBridge()
                data = {
                    "pending": brain.get_pending_requests(),
                    "activity": brain.get_activity_log(20),
                }
                self._send_json(data)
            except Exception as e:
                logger.warning(f"Brain endpoint error: {e}")
                self._send_json({"error": str(e)})
        elif self.path == '/workers':
            workers = _read_worker_live_files()
            self._send_json(workers)
        else:
            return super().do_GET()

    def log_message(self, format, *args):
        pass


def add_log(agent_id: str, text: str):
    global log_counter
    with lock:
        log_counter += 1
        entry = {
            "id": f"log_{log_counter}",
            "text": text,
            "time": datetime.now().strftime("%H:%M:%S"),
        }
        state = agent_states[agent_id]
        state["recent_logs"].append(entry)
        if len(state["recent_logs"]) > 60:
            state["recent_logs"] = state["recent_logs"][-60:]


def add_orch_thought(text: str, thought_type: str = "info"):
    global orch_counter
    with lock:
        orch_counter += 1
        orch_thinking.append({
            "id": f"orch_{orch_counter}",
            "text": text,
            "type": thought_type,
            "time": datetime.now().strftime("%H:%M:%S"),
        })
        if len(orch_thinking) > 200:
            del orch_thinking[:100]


def add_god_item(text: str, detail: str = "", critical: bool = False):
    global god_counter
    with lock:
        god_counter += 1
        god_queue.append({
            "id": f"god_{god_counter}",
            "text": text,
            "detail": detail,
            "critical": critical,
            "time": datetime.now().strftime("%H:%M:%S"),
        })


def _read_worker_live_files():
    """Read all worker live status files and return combined state."""
    workers = {}
    for i in range(4):
        live_file = QUEUE_DIR / f"worker_{i}_live.json"
        try:
            if live_file.exists():
                data = json.loads(live_file.read_text(encoding="utf-8"))
                workers[f"worker_{i}"] = data
        except (json.JSONDecodeError, OSError) as e:
            logger.debug(f"Failed to read {live_file}: {e}")
            workers[f"worker_{i}"] = {"status": "UNKNOWN", "worker_id": i}
    return workers


def _read_task_queue_status():
    """Read task queue and return summary stats."""
    try:
        if TASK_QUEUE_FILE.exists():
            tasks = json.loads(TASK_QUEUE_FILE.read_text(encoding="utf-8"))
            if isinstance(tasks, list):
                return {"size": len(tasks), "tasks": tasks[:10]}
    except (json.JSONDecodeError, OSError) as e:
        logger.debug(f"Failed to read task queue: {e}")
    return {"size": 0, "tasks": []}


def _collect_worker_results():
    """Collect and clean up worker result files."""
    results = []
    for result_file in sorted(QUEUE_DIR.glob("worker_*_result_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:20]:
        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
            results.append(data)
            result_file.unlink()
        except (json.JSONDecodeError, OSError, PermissionError) as e:
            logger.debug(f"Failed to collect result {result_file}: {e}")
    return results


def _process_result(agent_id, status, output, desc):
    """Process a task result — update agent state and generate logs."""
    with lock:
        state = agent_states[agent_id]
        state["progress"] = 100

    if status == "success":
        with lock:
            state = agent_states[agent_id]
            state["status"] = "DONE"
            state["tasks_completed"] = state.get("tasks_completed", 0) + 1
            state["current_task"] = None
        add_orch_thought(f"✓ COMPLETE ← {agent_id.upper()}: {desc} [SUCCESS]", "decide")
        for line in output.split("\n"):
            line = line.strip()
            if line:
                add_log(agent_id, line)
    else:
        with lock:
            agent_states[agent_id]["status"] = "IDLE"
            agent_states[agent_id]["current_task"] = None
        add_orch_thought(f"✗ FAILED ← {agent_id.upper()}: {desc}", "error")
        add_log(agent_id, f"✗ Task failed: {output[:100]}")

    def reset_idle(aid):
        time.sleep(3)
        with lock:
            if agent_states[aid]["status"] == "DONE":
                agent_states[aid]["status"] = "IDLE"
                agent_states[aid]["progress"] = 0
    threading.Thread(target=reset_idle, args=(agent_id,), daemon=True).start()


def _poll_orch_file(orch_file, last_orch_mtime):
    """Poll orchestrator thinking file for new entries. Returns updated mtime."""
    if not orch_file.exists():
        return last_orch_mtime
    try:
        mtime = orch_file.stat().st_mtime
        if mtime != last_orch_mtime:
            with open(orch_file, 'r') as f:
                entries = json.load(f)
            if isinstance(entries, list):
                with lock:
                    for entry in entries:
                        if not any(e.get("id") == entry.get("id") for e in orch_thinking):
                            orch_thinking.append(entry)
                    if len(orch_thinking) > 200:
                        del orch_thinking[:100]
            return mtime
    except (json.JSONDecodeError, OSError) as e:
        logger.debug(f"Failed to read orch thinking file: {e}")
    return last_orch_mtime


def _poll_god_file(god_file, last_god_mtime):
    """Poll GOD queue file for new entries. Returns updated mtime."""
    if not god_file.exists():
        return last_god_mtime
    try:
        mtime = god_file.stat().st_mtime
        if mtime != last_god_mtime:
            with open(god_file, 'r') as f:
                entries = json.load(f)
            if isinstance(entries, list):
                with lock:
                    for entry in entries:
                        if not any(e.get("id") == entry.get("id") for e in god_queue):
                            god_queue.append(entry)
            return mtime
    except (json.JSONDecodeError, OSError) as e:
        logger.debug(f"Failed to read god queue file: {e}")
    return last_god_mtime


def _poll_worker_results(last_results):
    """Scan for worker result files (worker_*_result_*.json) and process them."""
    AGENT_NAMES = ["alpha", "beta", "gamma", "delta"]
    for result_path in QUEUE_DIR.glob("worker_*_result_*.json"):
        try:
            mtime = result_path.stat().st_mtime
            if last_results.get(result_path.name) != mtime:
                last_results[result_path.name] = mtime
                with open(result_path, 'r') as f:
                    result = json.load(f)

                parts = result_path.stem.split('_')
                worker_id = int(parts[1])
                agent_id = AGENT_NAMES[worker_id] if worker_id < len(AGENT_NAMES) else AGENT_NAMES[0]

                _process_result(
                    agent_id,
                    result.get("status", "unknown"),
                    result.get("output", ""),
                    result.get("description", result.get("task_id", "task")),
                )

                try:
                    result_path.unlink()
                except OSError as e:
                    logger.debug(f"Failed to clean up {result_path}: {e}")
        except (json.JSONDecodeError, OSError, IndexError, ValueError) as e:
            logger.debug(f"Failed to process result {result_path}: {e}")


def _poll_legacy_results(last_results):
    """Check legacy per-agent result files."""
    AGENT_NAMES = ["alpha", "beta", "gamma", "delta"]
    for agent_id in AGENT_NAMES:
        result_file = QUEUE_DIR / f"{agent_id}_result.json"
        if not result_file.exists():
            continue
        try:
            mtime = result_file.stat().st_mtime
            key = f"{agent_id}_result"
            if last_results.get(key) != mtime:
                last_results[key] = mtime
                with open(result_file, 'r') as f:
                    result = json.load(f)
                _process_result(
                    agent_id,
                    result.get("status", "unknown"),
                    result.get("output", ""),
                    result.get("description", "task"),
                )
        except (json.JSONDecodeError, OSError) as e:
            logger.debug(f"Failed to process legacy result for {agent_id}: {e}")


def _poll_live_status(agent_id, last_live_times, last_worker_status):
    """Check live status files for a single agent."""
    worker_id = {"alpha": 0, "beta": 1, "gamma": 2, "delta": 3}[agent_id]
    status_file = QUEUE_DIR / f"worker_{worker_id}_live.json"
    legacy_status_file = QUEUE_DIR / f"{agent_id}_live.json"

    active_status_file = status_file if status_file.exists() else (legacy_status_file if legacy_status_file.exists() else None)
    if not active_status_file:
        return

    try:
        mtime = active_status_file.stat().st_mtime
        if last_live_times.get(agent_id) == mtime:
            return
        last_live_times[agent_id] = mtime
        with open(active_status_file, 'r') as f:
            live = json.load(f)

        prev_status = last_worker_status.get(agent_id, "IDLE")
        new_status = live.get("status", "IDLE")

        with lock:
            state = agent_states[agent_id]
            if live.get("status"):
                state["status"] = live["status"]
            if live.get("tasks_completed"):
                state["tasks_completed"] = live["tasks_completed"]
            if live.get("current_task"):
                state["current_task"] = live["current_task"]
            elif new_status == "IDLE":
                state["current_task"] = None
            lines = live.get("new_lines", [])
            if lines:
                state["progress"] = min(90, state.get("progress", 0) + len(lines) * 5)
            for line in lines:
                add_log(agent_id, line)
                add_orch_thought(f"  {agent_id.upper()} \u25b8 {line[:80]}", "info")

        if new_status == "WORKING" and prev_status != "WORKING":
            task_desc = live.get("current_task", "unknown task")
            add_log(agent_id, f"\u25b6 Working: {task_desc}")
            add_orch_thought(f"\u25b8 DISPATCH \u2192 {agent_id.upper()}: {task_desc}", "route")
            with lock:
                agent_states[agent_id]["progress"] = 10
        elif new_status == "IDLE" and prev_status == "WORKING":
            add_log(agent_id, f"\u2713 Completed task")
            with lock:
                agent_states[agent_id]["progress"] = 0

        last_worker_status[agent_id] = new_status
    except (json.JSONDecodeError, OSError) as e:
        logger.debug(f"Failed to read live status for {agent_id}: {e}")


def poll_agent_status():
    """Poll agent files at high frequency for real-time monitoring."""
    AGENT_NAMES = ["alpha", "beta", "gamma", "delta"]
    last_results = {}
    last_live_times = {}
    last_worker_status = {}
    last_orch_mtime = 0
    last_god_mtime = 0

    orch_file = QUEUE_DIR / "orch_thinking.json"
    god_file = QUEUE_DIR / "god_queue.json"

    while True:
        last_orch_mtime = _poll_orch_file(orch_file, last_orch_mtime)
        last_god_mtime = _poll_god_file(god_file, last_god_mtime)

        try:
            mtime = TASK_QUEUE_FILE.stat().st_mtime if TASK_QUEUE_FILE.exists() else 0
            if mtime and last_results.get("task_queue") != mtime:
                last_results["task_queue"] = mtime
                queue_status = _read_task_queue_status()
                if queue_status["size"] > 0:
                    add_orch_thought(f"\U0001f4cb QUEUE: {queue_status['size']} tasks pending", "route")
        except OSError as e:
            logger.debug(f"Failed to check task queue: {e}")

        _poll_worker_results(last_results)
        _poll_legacy_results(last_results)

        for agent_id in AGENT_NAMES:
            _poll_live_status(agent_id, last_live_times, last_worker_status)

        time.sleep(0.15)


def main():
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    # Start poller thread
    poller = threading.Thread(target=poll_agent_status, daemon=True)
    poller.start()

    # Initial orchestrator thoughts
    add_orch_thought("▸ BOOT Dashboard server v2 starting", "route")
    add_orch_thought("▸ INIT ATC Control Center online", "route")
    add_orch_thought("▸ INIT Skynet Console armed — authority routing enabled", "decide")
    add_orch_thought("▸ POLL Agent status polling at 150ms interval", "route")

    # Initial agent messages
    add_log("alpha", "Agent ALPHA online — Research & Intelligence")
    add_log("beta", "Agent BETA online — Code & Build")
    add_log("gamma", "Agent GAMMA online — Test & Deploy")
    add_log("delta", "Agent DELTA online — Monitor & Guardian")

    port = 8420
    server = ThreadedServer(('0.0.0.0', port), Handler)
    print(f"Skynet Control Center running at http://127.0.0.1:{port}")
    print("Skynet Console active — authority routing enabled")
    server.serve_forever()


if __name__ == "__main__":
    main()
