#!/usr/bin/env python3
"""
skynet_bus_worker.py -- Bus-based task delivery for Skynet workers.

Eliminates cross-window keyboard injection (ghost_type_to_worker) by having
each worker poll the bus for tasks addressed to it. When a task arrives, the
worker daemon types it into the worker's OWN chat window via self-input.

Architecture:
  - Orchestrator POSTs task to bus: {topic: "worker_alpha", type: "task", content: "..."}
  - This daemon polls bus every 3s for topic=worker_{name} type=task
  - When found, types the task into the worker's VS Code chat window
  - Marks task as consumed via bus publish (type=task_ack)

Usage:
    python tools/skynet_bus_worker.py alpha        # run for worker alpha
    python tools/skynet_bus_worker.py alpha --once  # single poll, then exit
    python tools/skynet_bus_worker.py alpha --dry-run  # show tasks without executing
"""

import argparse
import ctypes
import ctypes.wintypes
import json
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
WORKERS_FILE = DATA_DIR / "workers.json"
BUS_URL = "http://localhost:8420"
POLL_INTERVAL = 3  # seconds

user32 = ctypes.windll.user32

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(worker, msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [BUS-WORKER-{worker.upper()}] [{level}] {msg}", flush=True)


def _fetch_json(url, timeout=5):
    try:
        return json.loads(urllib.request.urlopen(url, timeout=timeout).read())
    except Exception:
        return None


def _post_bus(sender, topic, msg_type, content):
    try:
        payload = json.dumps({
            "sender": sender,
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


def _get_worker_hwnd(worker_name):
    """Get HWND for a worker from workers.json."""
    try:
        data = json.loads(WORKERS_FILE.read_text(encoding="utf-8"))
        workers = data.get("workers", [])
        for w in workers:
            if w.get("name") == worker_name:
                return w.get("hwnd", 0)
    except Exception:
        pass
    return 0


def _get_orch_hwnd():
    """Get orchestrator HWND."""
    try:
        data = json.loads((DATA_DIR / "orchestrator.json").read_text(encoding="utf-8"))
        return data.get("orchestrator_hwnd", 0)
    except Exception:
        return 0


def _is_window_alive(hwnd):
    return bool(user32.IsWindow(int(hwnd)))


def _get_worker_state(hwnd):
    """Get worker UIA state."""
    try:
        from uia_engine import UIAEngine
        engine = UIAEngine()
        result = engine.scan(int(hwnd))
        return getattr(result, "state", "UNKNOWN")
    except Exception:
        return "UNKNOWN"


def _ps_csharp_addtype():
    """Return the C# Add-Type block for Win32 focus/thread helpers."""
    return (
        'Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms\n'
        'Add-Type @"\n'
        'using System; using System.Runtime.InteropServices; using System.Text;\n'
        'public class BusWorkerType {{\n'
        '    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);\n'
        '    [DllImport("user32.dll")] public static extern IntPtr FindWindowEx(IntPtr p, IntPtr c, string cls, string w);\n'
        '    [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);\n'
        '    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);\n'
        '    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();\n'
        '    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);\n'
        '    [DllImport("user32.dll")] public static extern IntPtr SetFocus(IntPtr h);\n'
        '    public static bool FocusViaAttach(IntPtr target) {{\n'
        '        uint targetTid = GetWindowThreadProcessId(target, out _);\n'
        '        uint myTid = GetCurrentThreadId();\n'
        '        if (targetTid == 0) return false;\n'
        '        AttachThreadInput(myTid, targetTid, true);\n'
        '        SetFocus(target);\n'
        '        return true;\n'
        '    }}\n'
        '    public static void DetachThread(IntPtr target) {{\n'
        '        uint targetTid = GetWindowThreadProcessId(target, out _);\n'
        '        uint myTid = GetCurrentThreadId();\n'
        '        AttachThreadInput(myTid, targetTid, false);\n'
        '    }}\n'
        '}}\n'
        '"@\n'
    )


def _ps_find_edit_and_paste(hwnd, orch_hwnd, safe_text):
    """Return PS block that finds the bottommost Edit, pastes text, and sends Enter."""
    return (
        f'$hwnd = [IntPtr]{hwnd}\n'
        f'$orchHwnd = [IntPtr]{orch_hwnd}\n'
        '\n'
        '$wnd = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)\n'
        '$allEdits = $wnd.FindAll(\n'
        '    [System.Windows.Automation.TreeScope]::Descendants,\n'
        '    (New-Object System.Windows.Automation.PropertyCondition(\n'
        '        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,\n'
        '        [System.Windows.Automation.ControlType]::Edit\n'
        '    ))\n'
        ')\n'
        '$edit = $null\n'
        '$maxY = -1\n'
        'foreach ($e in $allEdits) {{\n'
        '    try {{\n'
        '        $r = $e.Current.BoundingRectangle\n'
        '        if ($r.Y -gt $maxY) {{ $maxY = $r.Y; $edit = $e }}\n'
        '    }} catch {{}}\n'
        '}}\n'
        '\n'
        'if ($edit) {{\n'
        '    $savedClip = $null\n'
        '    try {{ $savedClip = [System.Windows.Forms.Clipboard]::GetText() }} catch {{}}\n'
        '\n'
        f'    [System.Windows.Forms.Clipboard]::SetText("{safe_text}")\n'
        '    Start-Sleep -Milliseconds 50\n'
        '\n'
        '    $attached = [BusWorkerType]::FocusViaAttach($hwnd)\n'
        '    if ($attached) {{\n'
        '        try {{ $edit.SetFocus() }} catch {{}}\n'
        '        Start-Sleep -Milliseconds 80\n'
        '        [System.Windows.Forms.SendKeys]::SendWait("^v")\n'
        '        Start-Sleep -Milliseconds 80\n'
        '        [System.Windows.Forms.SendKeys]::SendWait("{{ENTER}}")\n'
        '        [BusWorkerType]::DetachThread($hwnd)\n'
        '        Write-Host "OK"\n'
        '    }} else {{\n'
        '        try {{ $edit.SetFocus() }} catch {{}}\n'
        '        [BusWorkerType]::SetForegroundWindow($hwnd)\n'
        '        Start-Sleep -Milliseconds 80\n'
        '        [System.Windows.Forms.SendKeys]::SendWait("^v")\n'
        '        Start-Sleep -Milliseconds 80\n'
        '        [System.Windows.Forms.SendKeys]::SendWait("{{ENTER}}")\n'
        '        [BusWorkerType]::SetForegroundWindow($orchHwnd)\n'
        '        Write-Host "OK"\n'
        '    }}\n'
        '\n'
        '    if ($savedClip -and $savedClip.Length -gt 0) {{\n'
        '        Start-Sleep -Milliseconds 50\n'
        '        try {{ [System.Windows.Forms.Clipboard]::SetText($savedClip) }} catch {{}}\n'
        '    }}\n'
        '}} else {{\n'
        '    Write-Host "NO_EDIT"\n'
        '}}\n'
    )


def _build_type_ps_script(hwnd, orch_hwnd, safe_text):
    """Build the complete PowerShell script for typing text into a chat window."""
    return _ps_csharp_addtype() + '\n' + _ps_find_edit_and_paste(hwnd, orch_hwnd, safe_text)


def _type_into_window(hwnd, text, orch_hwnd=None):
    """Type text into a chat window via clipboard paste."""
    if orch_hwnd is None:
        orch_hwnd = hwnd
    safe_text = text.replace("'", "''").replace('"', '`"').replace("\n", " ")
    ps = _build_type_ps_script(hwnd, orch_hwnd, safe_text)
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=20,
            creationflags=0x08000000  # CREATE_NO_WINDOW
        )
        return "OK" in r.stdout
    except Exception:
        return False


TASK_TRACKER_FILE = DATA_DIR / "bus_task_tracker.json"
COMPLETION_TIMEOUT = 600  # 10 minutes — task must produce a result within this


def _load_tracker():
    try:
        return json.loads(TASK_TRACKER_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"tasks": {}}


def _save_tracker(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TASK_TRACKER_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(TASK_TRACKER_FILE)


def _track_task(msg_id, worker, content):
    """Record a delivered task for completion tracking."""
    data = _load_tracker()
    data["tasks"][msg_id] = {
        "worker": worker,
        "content": content[:200],
        "status": "delivered",
        "delivered_at": datetime.now().isoformat(),
        "completed_at": None,
    }
    # Trim old completed entries (keep last 200)
    tasks = data["tasks"]
    if len(tasks) > 200:
        completed = {k: v for k, v in tasks.items() if v.get("status") == "completed"}
        if len(completed) > 100:
            sorted_completed = sorted(completed.items(), key=lambda x: x[1].get("completed_at", ""))
            for k, _ in sorted_completed[:len(completed) - 100]:
                del tasks[k]
    _save_tracker(data)


def _check_completions(worker):
    """Cross-check bus results against tracked tasks. Mark completed ones."""
    data = _load_tracker()
    pending = {k: v for k, v in data["tasks"].items()
               if v.get("worker") == worker and v.get("status") == "delivered"}
    if not pending:
        return 0, []

    # Check bus for results from this worker
    msgs = _fetch_json(f"{BUS_URL}/bus/messages?limit=30")
    if not msgs:
        return 0, []

    worker_results = [m for m in msgs if m.get("sender") == worker and m.get("type") == "result"]

    completed = 0
    timed_out = []
    now = datetime.now()

    for task_id, task in list(pending.items()):
        delivered_str = task.get("delivered_at", "")
        try:
            delivered_at = datetime.fromisoformat(delivered_str)
        except Exception:
            delivered_at = now

        # Check if any result from this worker appeared after delivery
        for r in worker_results:
            r_ts = r.get("timestamp", "")
            try:
                result_time = datetime.fromisoformat(r_ts.replace("+08:00", "").replace("Z", ""))
            except Exception:
                continue
            if result_time >= delivered_at:
                task["status"] = "completed"
                task["completed_at"] = result_time.isoformat()
                completed += 1
                break

        # Check for timeout
        if task["status"] == "delivered":
            age = (now - delivered_at).total_seconds()
            if age > COMPLETION_TIMEOUT:
                task["status"] = "timed_out"
                timed_out.append(task_id)

    data["tasks"].update(pending)
    _save_tracker(data)
    return completed, timed_out


class BusWorkerDaemon:
    """Polls bus for tasks addressed to this worker, executes them."""

    def __init__(self, worker_name, dry_run=False):
        self.name = worker_name
        self.topic = f"worker_{worker_name}"  # bus topic for this worker
        self.dry_run = dry_run
        self.consumed_ids = set()
        self.hwnd = _get_worker_hwnd(worker_name)
        self.orch_hwnd = _get_orch_hwnd()
        self.task_count = 0
        self.pid_file = DATA_DIR / f"bus_worker_{worker_name}.pid"
        self._completion_check_counter = 0

    def poll_tasks(self):
        """Poll bus for pending tasks. Returns list of task messages."""
        # Check both direct topic (worker_alpha) and the worker's name as topic
        tasks = []
        for topic in [self.topic, self.name]:
            msgs = _fetch_json(f"{BUS_URL}/bus/messages?limit=20&topic={topic}")
            if msgs and isinstance(msgs, list):
                for m in msgs:
                    if m.get("type") == "task" and m.get("id") not in self.consumed_ids:
                        tasks.append(m)
        return tasks

    def consume_task(self, task_msg):
        """Execute a task by typing it into the worker's chat window."""
        msg_id = task_msg.get("id", "?")
        content = task_msg.get("content", "")
        sender = task_msg.get("sender", "?")

        if not content:
            log(self.name, f"Empty task {msg_id}, skipping")
            return False

        self.consumed_ids.add(msg_id)

        log(self.name, f"Task from {sender}: {content[:80]}...")

        if self.dry_run:
            log(self.name, f"DRY RUN: would type into HWND {self.hwnd}")
            return True

        # Refresh HWND in case it changed
        self.hwnd = _get_worker_hwnd(self.name)
        if not self.hwnd or not _is_window_alive(self.hwnd):
            log(self.name, f"Window HWND {self.hwnd} dead, cannot deliver", "ERROR")
            _post_bus(f"bus_worker_{self.name}", "orchestrator", "delivery_fail",
                      f"BUS_WORKER_{self.name.upper()}: Window dead, task undeliverable: {content[:60]}")
            return False

        # Check worker state -- don't interrupt if STEERING
        state = _get_worker_state(self.hwnd)
        if state == "STEERING":
            log(self.name, "Worker in STEERING state, deferring task", "WARNING")
            self.consumed_ids.discard(msg_id)  # retry later
            return False

        # Type task into worker's chat
        ok = _type_into_window(self.hwnd, content, self.orch_hwnd)

        if ok:
            self.task_count += 1
            # COMPLETION GATE: track this task for result verification
            _track_task(msg_id, self.name, content)
            _post_bus(f"bus_worker_{self.name}", self.topic, "task_ack",
                      f"Task {msg_id} delivered to {self.name.upper()}")
            log(self.name, f"Task DELIVERED + TRACKED (#{self.task_count})")
        else:
            log(self.name, "Task delivery FAILED", "ERROR")
            self.consumed_ids.discard(msg_id)  # retry

        return ok

    def run_once(self):
        """Single poll cycle."""
        tasks = self.poll_tasks()
        if tasks:
            log(self.name, f"Found {len(tasks)} task(s)")
            for t in tasks:
                self.consume_task(t)
        else:
            log(self.name, "No pending tasks")
        return tasks

    def run(self):
        """Continuous polling daemon."""
        log(self.name, f"Bus worker daemon starting (HWND={self.hwnd}, poll={POLL_INTERVAL}s)")
        _post_bus(f"bus_worker_{self.name}", "orchestrator", "monitor_alert",
                  f"BUS_WORKER_{self.name.upper()}_ONLINE: Polling daemon started")

        # Write PID file
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text(str(os.getpid()))

        try:
            while True:
                try:
                    tasks = self.poll_tasks()
                    for t in tasks:
                        self.consume_task(t)
                        time.sleep(1)  # gap between tasks

                    # COMPLETION GATE: check every 10th cycle (~30s)
                    self._completion_check_counter += 1
                    if self._completion_check_counter >= 10:
                        self._completion_check_counter = 0
                        completed, timed_out = _check_completions(self.name)
                        if completed:
                            log(self.name, f"Completion gate: {completed} task(s) confirmed done")
                        for tid in timed_out:
                            log(self.name, f"COMPLETION GATE TIMEOUT: task {tid}", "WARNING")
                            _post_bus(f"bus_worker_{self.name}", "orchestrator", "completion_timeout",
                                      f"TASK_TIMEOUT: {self.name.upper()} task {tid} got no result in {COMPLETION_TIMEOUT}s")
                except Exception as e:
                    log(self.name, f"Poll error: {e}", "ERROR")
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log(self.name, "Shutting down")
        finally:
            _post_bus(f"bus_worker_{self.name}", "orchestrator", "monitor_alert",
                      f"BUS_WORKER_{self.name.upper()}_OFFLINE: Daemon stopped")
            if self.pid_file.exists():
                try:
                    self.pid_file.unlink()
                except Exception:
                    pass


def dispatch_via_bus(target_worker, task_content, sender="orchestrator"):
    """Dispatch a task to a worker via the bus (no UIA injection from caller).
    
    This is the replacement for ghost_type_to_worker — the task goes to the bus,
    and the worker's local bus_worker daemon picks it up and self-injects.
    """
    topic = f"worker_{target_worker}"
    return _post_bus(sender, topic, "task", task_content)


def main():
    parser = argparse.ArgumentParser(description="Skynet Bus Worker -- Poll-based task delivery")
    parser.add_argument("worker", help="Worker name (alpha, beta, gamma, delta)")
    parser.add_argument("--once", action="store_true", help="Single poll then exit")
    parser.add_argument("--dry-run", action="store_true", help="Show tasks without executing")
    args = parser.parse_args()

    name = args.worker.lower()
    if name not in ("alpha", "beta", "gamma", "delta"):
        print(f"Unknown worker: {name}", file=sys.stderr)
        sys.exit(1)

    daemon = BusWorkerDaemon(name, dry_run=args.dry_run)

    if args.once:
        daemon.run_once()
    else:
        daemon.run()


if __name__ == "__main__":
    main()
