#!/usr/bin/env python3
"""
Skynet Real-Time — UIA-based result extraction and worker lifecycle management.

Replaces bus-polling with direct UIA observation:
  1. Poll worker state every 1-2s via COM UIA
  2. Detect PROCESSING → IDLE transition = task complete
  3. Extract last response text from UIA accessibility tree
  4. Auto-recover UNKNOWN/dead workers via new-chat

No dependency on workers posting to bus. Works with any LLM chat window.

Usage:
    from tools.skynet_realtime import RealtimeCollector
    collector = RealtimeCollector()
    results = collector.collect(["alpha", "beta"], timeout=120)
    # results = {"alpha": "response text...", "beta": "response text..."}
"""

import ctypes
import ctypes.wintypes
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

WORKERS_FILE = ROOT / "data" / "workers.json"
NEW_CHAT_SCRIPT = ROOT / "tools" / "new_chat.ps1"

user32 = ctypes.windll.user32


def log(msg, level="SYS"):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"OK": "\u2705", "ERR": "\u274c", "WARN": "\u26a0\ufe0f", "SYS": "\u2699\ufe0f"}.get(level, "\u2699\ufe0f")
    print(f"[{ts}] {prefix} {msg}", flush=True)


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


def _hidden_run(args, **kwargs):
    return subprocess.run(args, **_hidden_subprocess_kwargs(**kwargs))


def _load_workers():
    if not WORKERS_FILE.exists():
        return [], None
    data = json.loads(WORKERS_FILE.read_text())
    return data.get("workers", []), data.get("orchestrator_hwnd")


# ─── UIA Text Extraction ───────────────────────────────────────────────────

def extract_last_response(hwnd, max_chars=2000):
    """Extract the last chat response text from a worker window via UIA.

    Strategy: Read ListItem elements from the chat conversation.
    Chat responses appear as ListItem with substantial Name text.
    Returns the concatenated text of the last response block.
    Uses COM UIA directly for speed (~50-100ms).
    """
    items = _get_listitem_snapshot(hwnd)
    if not items:
        return None

    # Sort by Y position (top to bottom), take the last response block
    items.sort(key=lambda x: x[0])

    # Heuristic: the last few ListItems are the response
    # Skip items that look like command previews ("Ran terminal command:")
    response_parts = []
    for y, text in reversed(items):
        if text.startswith("Ran terminal command:") or text.startswith("Ran command:"):
            break
        response_parts.insert(0, text)
        if len("\n".join(response_parts)) > max_chars:
            break

    if not response_parts:
        response_parts = [items[-1][1]]

    result = "\n".join(response_parts)
    return result[:max_chars] if len(result) > max_chars else result


def extract_new_response(hwnd, baseline_hash, max_chars=2000):
    """Extract response text only if it changed since baseline_hash.

    Uses content hash (not count) because chat responses update existing
    ListItems rather than creating new ones.
    Returns (text, current_hash) — text is None if content unchanged.
    """
    items = _get_listitem_snapshot(hwnd)
    if not items:
        return None, ""

    items.sort(key=lambda x: x[0])

    # Hash the last item's content
    last_text = items[-1][1] if items else ""
    import hashlib
    current_hash = hashlib.md5(last_text.encode(errors="replace")).hexdigest()[:12]

    if current_hash == baseline_hash:
        return None, current_hash  # Content unchanged

    # Content changed — extract the response
    response_parts = []
    for _, text in reversed(items):
        if text.startswith("Ran terminal command:") or text.startswith("Ran command:"):
            break
        response_parts.insert(0, text)
        if len("\n".join(response_parts)) > max_chars:
            break

    if not response_parts:
        response_parts = [items[-1][1]]

    result = "\n".join(response_parts)
    return result[:max_chars] if len(result) > max_chars else result, current_hash


def get_conversation_hash(hwnd):
    """Fast content hash of last ListItem for conversation fingerprinting (~30ms)."""
    items = _get_listitem_snapshot(hwnd)
    if not items:
        return ""
    items.sort(key=lambda x: x[0])
    last_text = items[-1][1] if items else ""
    import hashlib
    return hashlib.md5(last_text.encode(errors="replace")).hexdigest()[:12]


def _get_listitem_snapshot(hwnd):
    """Get all ListItem (y, name) tuples from a window. Shared by all extraction functions."""
    try:
        import comtypes
        import comtypes.client
        from comtypes.gen import UIAutomationClient as UIA

        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass

        uia = comtypes.CoCreateInstance(
            comtypes.GUID("{ff48dba4-60ef-4201-aa87-54103eef594e}"),
            interface=UIA.IUIAutomation,
            clsctx=comtypes.CLSCTX_INPROC_SERVER,
        )

        root = uia.ElementFromHandle(ctypes.c_void_p(hwnd))
        if not root:
            return []

        li_cond = uia.CreatePropertyCondition(30003, 50007)  # ControlType.ListItem
        li_els = root.FindAll(4, li_cond)  # TreeScope.Descendants

        items = []
        for i in range(li_els.Length):
            el = li_els.GetElement(i)
            name = el.CurrentName or ""
            if name.strip() and len(name) > 5:
                try:
                    rect = el.CurrentBoundingRectangle
                    y = rect.top
                except Exception:
                    y = 0
                items.append((y, name.strip()))

        return items

    except Exception as e:
        log(f"UIA ListItem scan failed for HWND={hwnd}: {e}", "WARN")
        return []


# ─── Worker Recovery ────────────────────────────────────────────────────────

def recover_worker(worker_name, workers_data, orch_hwnd):
    """Attempt to recover an UNKNOWN/dead worker.

    Phase 1: Check if window still exists (IsWindowVisible)
    Phase 2: If dead, spawn new-chat and assign to worker slot
    Returns (success: bool, new_hwnd: int or None)
    """
    target = None
    for w in workers_data:
        if w["name"] == worker_name:
            target = w
            break

    if not target:
        log(f"Worker {worker_name} not in workers.json", "ERR")
        return False, None

    hwnd = target["hwnd"]

    # Phase 1: Check if window is visible
    if user32.IsWindowVisible(hwnd):
        # Window exists but state is UNKNOWN — try UIA re-scan
        from tools.uia_engine import get_engine
        scan = get_engine().scan(hwnd)
        if scan.state != "UNKNOWN":
            log(f"Recovery: {worker_name.upper()} recovered — state is now {scan.state}", "OK")
            return True, hwnd

        # Model/agent might be wrong — fix it
        if not scan.model_ok or not scan.agent_ok:
            log(f"Recovery: {worker_name.upper()} has wrong model/agent — needs reconfiguration", "WARN")
            # Let the model guard in new_chat handle this via dispatch
            return True, hwnd

        log(f"Recovery: {worker_name.upper()} window visible but state still UNKNOWN", "WARN")
        return True, hwnd

    # Phase 2: Window is dead — spawn new chat
    log(f"Recovery: {worker_name.upper()} window DEAD (HWND={hwnd}) — spawning new chat", "SYS")
    return _spawn_new_chat(worker_name, workers_data, orch_hwnd)


def _spawn_new_chat(worker_name, workers_data, orch_hwnd):
    """Spawn a new chat window via new_chat.ps1 and assign it to the worker slot."""
    if not NEW_CHAT_SCRIPT.exists():
        log(f"new_chat.ps1 not found at {NEW_CHAT_SCRIPT}", "ERR")
        return False, None

    try:
        # Snapshot existing VS Code windows
        before_hwnds = _get_vscode_hwnds()

        result = _hidden_run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(NEW_CHAT_SCRIPT)],
            capture_output=True, text=True, timeout=30, cwd=str(ROOT)
        )

        if result.returncode != 0:
            log(f"new_chat.ps1 failed: {result.stderr[:200]}", "ERR")
            return False, None

        # Extract HWND from output: "OK HWND=12345 ..."
        output = result.stdout
        import re
        m = re.search(r'HWND=(\d+)', output)
        if not m:
            # Try to find new window by diff
            after_hwnds = _get_vscode_hwnds()
            new_hwnds = [h for h in after_hwnds if h not in before_hwnds]
            if new_hwnds:
                new_hwnd = new_hwnds[0]
            else:
                log(f"Could not identify new chat window HWND", "ERR")
                return False, None
        else:
            new_hwnd = int(m.group(1))

        # Update workers.json with new HWND
        _update_worker_hwnd(worker_name, new_hwnd)
        log(f"Recovery: {worker_name.upper()} → new HWND={new_hwnd}", "OK")
        return True, new_hwnd

    except subprocess.TimeoutExpired:
        log(f"new_chat.ps1 timed out after 30s", "ERR")
        return False, None
    except Exception as e:
        log(f"Spawn failed: {e}", "ERR")
        return False, None


def _get_vscode_hwnds():
    """Get all visible VS Code window HWNDs."""
    hwnds = []
    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if "Visual Studio Code" in buf.value:
                hwnds.append(hwnd)
        return True
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return hwnds


def _update_worker_hwnd(worker_name, new_hwnd):
    """Update workers.json with a new HWND for a worker."""
    if not WORKERS_FILE.exists():
        return
    data = json.loads(WORKERS_FILE.read_text())
    for w in data.get("workers", []):
        if w["name"] == worker_name:
            old_hwnd = w["hwnd"]
            w["hwnd"] = new_hwnd
            log(f"workers.json: {worker_name} HWND {old_hwnd} → {new_hwnd}", "SYS")
            break
    WORKERS_FILE.write_text(json.dumps(data, indent=2))


# ─── Worker Scoring ─────────────────────────────────────────────────────────

SCORE_FILE = ROOT / "data" / "worker_scores.json"


def _load_scores():
    if SCORE_FILE.exists():
        return json.loads(SCORE_FILE.read_text())
    return {}


def _save_scores(scores):
    SCORE_FILE.parent.mkdir(exist_ok=True)
    SCORE_FILE.write_text(json.dumps(scores, indent=2))


def record_outcome(worker_name, success, elapsed_s, task_type="general"):
    """Record task outcome for worker scoring — powers smart routing."""
    scores = _load_scores()
    key = worker_name
    if key not in scores:
        scores[key] = {"total": 0, "success": 0, "fail": 0, "avg_time": 0.0, "history": []}

    s = scores[key]
    s["total"] += 1
    if success:
        s["success"] += 1
    else:
        s["fail"] += 1
    # Rolling average time (last 20)
    s["history"].append({"ok": success, "time": round(elapsed_s, 1), "type": task_type, "ts": time.time()})
    s["history"] = s["history"][-20:]  # Keep last 20
    times = [h["time"] for h in s["history"] if h["ok"]]
    s["avg_time"] = round(sum(times) / len(times), 1) if times else 0.0
    s["success_rate"] = round(s["success"] / s["total"] * 100, 1)

    _save_scores(scores)
    return s


def get_best_workers(count=4):
    """Rank workers by reliability — used by smart dispatch routing."""
    scores = _load_scores()
    workers, _ = _load_workers()
    names = [w["name"] for w in workers]

    ranked = []
    for name in names:
        s = scores.get(name, {"success_rate": 50.0, "avg_time": 30.0, "total": 0})
        # Score = success_rate * 0.7 + speed_score * 0.3
        speed_score = max(0, 100 - s.get("avg_time", 30))
        composite = s.get("success_rate", 50) * 0.7 + speed_score * 0.3
        ranked.append((name, composite, s.get("total", 0)))

    ranked.sort(key=lambda x: (-x[1], x[0]))
    return [r[0] for r in ranked[:count]]


# ─── Real-Time Collector v3 ────────────────────────────────────────────────

class RealtimeCollector:
    """Collect worker results via UIA with conversation fingerprinting.

    v3 upgrades over v1:
    1. Conversation fingerprinting — snapshot ListItem count before dispatch,
       only extract NEW items after, eliminating stale response pollution
    2. Parallel text extraction — ThreadPoolExecutor across workers
    3. Auto-retry — stale/timeout workers get re-dispatched to idle workers
    4. Worker scoring — records success/failure for smart future routing
    5. Adaptive polling — 0.5s for first 5s (catch fast transitions), then 2s
    """

    def __init__(self, poll_interval=2.0, auto_recover=True):
        self.poll_interval = poll_interval
        self.auto_recover = auto_recover
        self._workers, self._orch_hwnd = _load_workers()
        self._worker_map = {w["name"]: w for w in self._workers}
        self._baselines = {}  # worker_name → ListItem count at dispatch time

    def snapshot_baselines(self, worker_names):
        """Take conversation fingerprint BEFORE dispatch.

        Stores content hash of last ListItem per worker. collect() then
        compares hashes to detect response changes (freshness guarantee).
        """
        from concurrent.futures import ThreadPoolExecutor
        def _get_hash(name):
            if name not in self._worker_map:
                return name, ""
            return name, get_conversation_hash(self._worker_map[name]["hwnd"])

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_get_hash, n): n for n in worker_names}
            for f in futures:
                name, h = f.result()
                self._baselines[name] = h

        log(f"  Baselines: {', '.join(f'{n}={c[:8]}' for n, c in self._baselines.items())}", "SYS")

    def collect(self, expected_workers, timeout=120, extract_text=True, task_types=None):
        """Real-time result collection with fingerprinting.

        Args:
            expected_workers: list of worker names dispatched to
            timeout: max seconds to wait
            extract_text: if True, extract response text via UIA when IDLE
            task_types: optional dict of worker_name → task_type for scoring

        Returns: dict of worker_name → {status, text, elapsed_s, fresh}
        """
        from tools.uia_engine import get_engine
        from concurrent.futures import ThreadPoolExecutor
        engine = get_engine()

        results = {}
        prev_states = {}
        saw_processing = set()
        deadline = time.time() + timeout
        t0 = time.time()
        recovery_attempted = set()
        task_types = task_types or {}

        # Initial state snapshot
        for name in expected_workers:
            if name in self._worker_map:
                state = engine.get_state(self._worker_map[name]["hwnd"])
                prev_states[name] = state
                if state == "PROCESSING":
                    saw_processing.add(name)
                log(f"  {name.upper()}: initial={state} baseline={self._baselines.get(name, '?')}", "SYS")

        poll_count = 0
        while time.time() < deadline:
            poll_count += 1
            remaining = [w for w in expected_workers if w not in results]
            if not remaining:
                break

            hwnds_to_scan = {n: self._worker_map[n]["hwnd"] for n in remaining if n in self._worker_map}
            if not hwnds_to_scan:
                break

            scans = engine.scan_all(hwnds_to_scan)

            # Adaptive polling — fast in first 5s to catch quick transitions
            elapsed_total = time.time() - t0
            poll_sleep = 0.5 if elapsed_total < 5.0 else self.poll_interval

            for name, scan_result in scans.items():
                prev = prev_states.get(name, "UNKNOWN")
                curr = scan_result.state

                if curr == "PROCESSING":
                    saw_processing.add(name)

                # DONE: IDLE after PROCESSING
                if curr == "IDLE" and name in saw_processing:
                    elapsed = time.time() - t0
                    log(f"  {name.upper()}: DONE ({prev}→IDLE {elapsed:.1f}s)", "OK")
                    text, fresh = self._extract_fresh(name) if extract_text else (None, False)
                    results[name] = {"status": "complete", "text": text, "elapsed_s": round(elapsed, 1), "fresh": fresh}
                    record_outcome(name, bool(text), elapsed, task_types.get(name, "general"))

                # Instant completion: IDLE but never saw PROCESSING + new ListItems exist
                elif curr == "IDLE" and name not in saw_processing and elapsed_total > 8:
                    _, fresh = self._check_freshness(name)
                    if fresh:
                        elapsed = time.time() - t0
                        text, _ = self._extract_fresh(name) if extract_text else (None, False)
                        log(f"  {name.upper()}: instant completion ({elapsed:.1f}s, fresh={fresh})", "OK")
                        results[name] = {"status": "complete", "text": text, "elapsed_s": round(elapsed, 1), "fresh": True}
                        record_outcome(name, bool(text), elapsed, task_types.get(name, "general"))
                    elif elapsed_total > 30:
                        log(f"  {name.upper()}: 30s IDLE, no new items — stale", "WARN")
                        results[name] = {"status": "stale", "text": None, "elapsed_s": round(time.time() - t0, 1), "fresh": False}
                        record_outcome(name, False, time.time() - t0, task_types.get(name, "general"))

                # UNKNOWN → recovery
                elif curr == "UNKNOWN" and self.auto_recover and name not in recovery_attempted:
                    recovery_attempted.add(name)
                    log(f"  {name.upper()}: UNKNOWN — recovering", "WARN")
                    ok, new_hwnd = recover_worker(name, self._workers, self._orch_hwnd)
                    if ok and new_hwnd:
                        self._worker_map[name]["hwnd"] = new_hwnd
                        self._workers = [w if w["name"] != name else {**w, "hwnd": new_hwnd} for w in self._workers]
                        log(f"  {name.upper()}: recovered HWND={new_hwnd}", "OK")

                prev_states[name] = curr

            if poll_count % 10 == 0:
                missing = [w for w in expected_workers if w not in results]
                states_str = ", ".join(f"{n}={prev_states.get(n, '?')}" for n in missing)
                log(f"  Waiting ({int(deadline - time.time())}s left): {states_str}", "SYS")

            time.sleep(poll_sleep)

        # Mark timed-out workers
        for name in expected_workers:
            if name not in results:
                results[name] = {
                    "status": "timeout", "text": None,
                    "elapsed_s": round(time.time() - t0, 1),
                    "last_state": prev_states.get(name, "UNKNOWN"),
                    "fresh": False,
                }
                log(f"  {name.upper()}: TIMEOUT ({prev_states.get(name, '?')})", "WARN")
                record_outcome(name, False, time.time() - t0, task_types.get(name, "general"))

        return results

    def _extract_fresh(self, name):
        """Extract response text and check if it changed vs baseline hash."""
        hwnd = self._worker_map[name]["hwnd"]
        baseline_hash = self._baselines.get(name, "")

        if baseline_hash:
            text, current_hash = extract_new_response(hwnd, baseline_hash)
            if text:
                log(f"  {name.upper()}: FRESH (hash {baseline_hash[:6]}→{current_hash[:6]}), {len(text)} chars", "OK")
                _bus_post(name, text[:500])
                return text, True

        # Fallback to full extraction
        text = extract_last_response(hwnd)
        if text:
            log(f"  {name.upper()}: fallback extraction {len(text)} chars", "OK")
            _bus_post(name, text[:500])
        return text, False

    def _check_freshness(self, name):
        """Check if worker response changed vs baseline (without full extraction)."""
        hwnd = self._worker_map[name]["hwnd"]
        baseline_hash = self._baselines.get(name, "")
        if not baseline_hash:
            return "", False
        current_hash = get_conversation_hash(hwnd)
        return current_hash, current_hash != baseline_hash

    def collect_with_retry(self, expected_workers, tasks, timeout=120, max_retries=1, dispatch_fn=None):
        """Collect with automatic retry for stale/timeout workers.

        If a worker fails, re-dispatch its task to the best idle worker.

        Args:
            expected_workers: list of worker names
            tasks: dict of worker_name → task_text
            timeout: per-round timeout
            max_retries: how many retry rounds
            dispatch_fn: callable(worker_name, task_text) to re-dispatch
        """
        all_results = {}
        pending_tasks = dict(tasks)  # worker → task

        for attempt in range(1 + max_retries):
            workers_this_round = [w for w in pending_tasks if w not in all_results or
                                  all_results[w]["status"] in ("stale", "timeout")]

            if not workers_this_round:
                break

            if attempt > 0:
                log(f"  Retry round {attempt}: {workers_this_round}", "SYS")
                # Re-snapshot baselines BEFORE re-dispatch
                self.snapshot_baselines(workers_this_round)
                # Re-dispatch all retries
                if dispatch_fn:
                    for w in workers_this_round:
                        dispatch_fn(w, pending_tasks[w])
                    time.sleep(3)  # Let all dispatches complete and LLMs start processing

            results = self.collect(workers_this_round, timeout=timeout)
            all_results.update(results)

            # Check if any failed and need retry
            failed = [w for w, r in results.items() if r["status"] in ("stale", "timeout")]
            if not failed:
                break

            # Find idle workers to absorb retries
            idle_workers = [w for w in self._worker_map if w not in pending_tasks and
                          w not in all_results]
            for i, fw in enumerate(failed):
                if i < len(idle_workers):
                    # Move task to idle worker
                    iw = idle_workers[i]
                    pending_tasks[iw] = pending_tasks.pop(fw)
                    log(f"  Retry: {fw.upper()} task → {iw.upper()}", "SYS")

        return all_results

    def wait_for_idle_all(self, workers, timeout=120):
        """Wait until all specified workers reach IDLE state."""
        from tools.uia_engine import get_engine
        engine = get_engine()
        deadline = time.time() + timeout
        done = set()

        while time.time() < deadline and len(done) < len(workers):
            for name in workers:
                if name in done:
                    continue
                if name in self._worker_map:
                    state = engine.get_state(self._worker_map[name]["hwnd"])
                    if state == "IDLE":
                        done.add(name)
                        log(f"  {name.upper()}: IDLE", "OK")
            time.sleep(self.poll_interval)

        return {w: (w in done) for w in workers}


def _bus_post(sender, content):
    """Post extracted result to bus for other systems."""
    try:
        import urllib.request
        body = json.dumps({
            "sender": sender,
            "topic": "orchestrator",
            "type": "result",
            "content": content,
        }).encode()
        req = urllib.request.Request(
            "http://localhost:8420/bus/publish",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass  # Bus post is best-effort


# ─── Worker Health Monitor ──────────────────────────────────────────────────

class WorkerHealthMonitor:
    """Continuous background health monitor for all workers.

    Runs in a daemon thread, checks every interval:
    - Worker window alive (IsWindowVisible)
    - Model/agent correctness
    - Auto-recover dead workers
    - Post health status to bus
    """

    def __init__(self, interval=15.0, auto_recover=True):
        self.interval = interval
        self.auto_recover = auto_recover
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self.health_log = []
        self.recovery_count = 0

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log("WorkerHealthMonitor started", "SYS")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        while self._running:
            try:
                self._check_all()
            except Exception as e:
                log(f"Health monitor error: {e}", "ERR")
            time.sleep(self.interval)

    def _check_all(self):
        from tools.uia_engine import get_engine
        engine = get_engine()
        workers, orch_hwnd = _load_workers()

        hwnds = {w["name"]: w["hwnd"] for w in workers}
        scans = engine.scan_all(hwnds)

        for name, scan_result in scans.items():
            status = {
                "worker": name,
                "state": scan_result.state,
                "model_ok": scan_result.model_ok,
                "agent_ok": scan_result.agent_ok,
                "visible": user32.IsWindowVisible(hwnds[name]),
                "time": datetime.now().isoformat(),
            }

            with self._lock:
                self.health_log.append(status)
                if len(self.health_log) > 200:
                    self.health_log = self.health_log[-200:]

            # Auto-recover dead workers
            if scan_result.state == "UNKNOWN" and not user32.IsWindowVisible(hwnds[name]):
                if self.auto_recover:
                    log(f"HealthMonitor: {name.upper()} DEAD — auto-recovering", "WARN")
                    ok, new_hwnd = recover_worker(name, workers, orch_hwnd)
                    if ok:
                        self.recovery_count += 1
                        _bus_post("monitor", f"Auto-recovered {name} (HWND={new_hwnd})")

            # Warn about model/agent drift
            if scan_result.state == "IDLE" and (not scan_result.model_ok or not scan_result.agent_ok):
                log(f"HealthMonitor: {name.upper()} model/agent drift — model_ok={scan_result.model_ok} agent_ok={scan_result.agent_ok}", "WARN")

    def get_status(self):
        with self._lock:
            return {
                "running": self._running,
                "log_entries": len(self.health_log),
                "recovery_count": self.recovery_count,
                "last_check": self.health_log[-1] if self.health_log else None,
            }


# ─── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Skynet Real-Time — UIA result extraction & worker health")
    parser.add_argument("--collect", type=str, help="Collect results from workers (comma-separated names)")
    parser.add_argument("--timeout", type=int, default=120, help="Collection timeout in seconds")
    parser.add_argument("--extract", type=str, help="Extract last response from a specific worker")
    parser.add_argument("--recover", type=str, help="Recover a specific dead worker")
    parser.add_argument("--health", action="store_true", help="Run single health check on all workers")
    parser.add_argument("--monitor", action="store_true", help="Start continuous health monitor (blocking)")
    parser.add_argument("--interval", type=float, default=15.0, help="Monitor check interval in seconds")
    args = parser.parse_args()

    if args.collect:
        names = [n.strip() for n in args.collect.split(",")]
        collector = RealtimeCollector()
        results = collector.collect(names, timeout=args.timeout)
        print(json.dumps(results, indent=2, default=str))

    elif args.extract:
        workers, _ = _load_workers()
        wmap = {w["name"]: w for w in workers}
        if args.extract in wmap:
            text = extract_last_response(wmap[args.extract]["hwnd"])
            if text:
                print(f"[{args.extract.upper()} response — {len(text)} chars]")
                print(text)
            else:
                print(f"No response text found for {args.extract.upper()}")
        else:
            print(f"Worker '{args.extract}' not found")

    elif args.recover:
        workers, orch_hwnd = _load_workers()
        ok, new_hwnd = recover_worker(args.recover, workers, orch_hwnd)
        print(f"Recovery {'OK' if ok else 'FAILED'}" + (f" (HWND={new_hwnd})" if new_hwnd else ""))

    elif args.health:
        from tools.uia_engine import get_engine
        engine = get_engine()
        workers, _ = _load_workers()
        hwnds = {w["name"]: w["hwnd"] for w in workers}
        scans = engine.scan_all(hwnds)
        print(f"\n{'='*60}")
        print(f"  SKYNET WORKER HEALTH CHECK")
        print(f"{'='*60}")
        for name in sorted(scans.keys()):
            s = scans[name]
            visible = user32.IsWindowVisible(hwnds[name])
            v_icon = "\u2705" if visible else "\u274c"
            m_icon = "\u2705" if s.model_ok else "\u274c"
            a_icon = "\u2705" if s.agent_ok else "\u274c"
            state_icon = {
                "IDLE": "\u2705", "PROCESSING": "\u23f3",
                "STEERING": "\u26a0\ufe0f", "UNKNOWN": "\u274c"
            }.get(s.state, "\u2753")
            print(f"  {name.upper():<8} {state_icon} {s.state:<12} visible={v_icon} model={m_icon} agent={a_icon} ({s.scan_ms:.0f}ms)")
        print(f"{'='*60}\n")

    elif args.monitor:
        monitor = WorkerHealthMonitor(interval=args.interval)
        monitor.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping monitor...")
            monitor.stop()

    else:
        parser.print_help()
