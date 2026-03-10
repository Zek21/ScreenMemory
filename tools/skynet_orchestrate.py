#!/usr/bin/env python3
"""
Skynet Orchestrate — Master orchestration pipeline.

Decomposes user prompts into worker-sized sub-tasks, dispatches them,
collects results from the bus, and returns a synthesis.

Usage:
    python tools/skynet_orchestrate.py --prompt "Audit all endpoints and scan for stubs"
    python tools/skynet_orchestrate.py --decompose-only --prompt "Review core/ and tools/"
    python tools/skynet_orchestrate.py --prompt "Run benchmarks" --timeout 180
"""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from tools.skynet_dispatch import (
    dispatch_to_worker,
    dispatch_parallel,
    scan_all_states,
    load_workers,
    load_orch_hwnd,
    smart_dispatch,
)
from tools.skynet_realtime import RealtimeCollector, recover_worker
from tools.skynet_identity_guard import get_orchestrator_guard

SKYNET_URL = "http://localhost:8420"
WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]


def log(msg, level="SYS"):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"OK": "\u2705", "ERR": "\u274c", "WARN": "\u26a0\ufe0f", "SYS": "\u2699\ufe0f"}.get(level, "\u2699\ufe0f")
    print(f"[{ts}] {prefix} {msg}")


def bus_post(sender, topic, msg_type, content):
    """Post a message to the Skynet bus."""
    body = json.dumps({"sender": sender, "topic": topic, "type": msg_type, "content": content}).encode()
    try:
        req = Request(f"{SKYNET_URL}/bus/publish", data=body, headers={"Content-Type": "application/json"})
        urlopen(req, timeout=5)
    except Exception as e:
        log(f"Bus post failed: {e}", "WARN")


def bus_messages(limit=50, topic=None):
    """Read messages from the Skynet bus."""
    url = f"{SKYNET_URL}/bus/messages?limit={limit}"
    if topic:
        url += f"&topic={topic}"
    try:
        return json.loads(urlopen(url, timeout=5).read())
    except Exception:
        return []


class SkynetOrchestrator:
    """Master orchestration pipeline: decompose → dispatch → collect → synthesize."""

    def __init__(self):
        self.workers = load_workers()
        self.orch_hwnd = load_orch_hwnd()
        self.worker_names = [w["name"] for w in self.workers]
        self._idle_cache = None
        self._idle_cache_ts = 0
        self._idle_cache_ttl = 5  # seconds

    def decompose_task(self, prompt):
        """Split a user prompt into worker-sized sub-tasks.

        Uses SmartDecomposer (worker-built) for keyword analysis, complexity
        estimation, and type classification. Falls back to heuristic rules
        for explicit worker routing (alpha: X, beta: Y).
        """
        prompt_lower = prompt.lower()

        # 1. Explicit worker routing: "alpha: do X, beta: do Y" — handle directly
        explicit = re.findall(r'\b(alpha|beta|gamma|delta)\s*:\s*(.+?)(?=\b(?:alpha|beta|gamma|delta)\s*:|$)', prompt, re.IGNORECASE)
        if explicit:
            subtasks = []
            for worker, task in explicit:
                subtasks.append({"worker": worker.lower(), "task": task.strip(), "priority": 5})
            return subtasks

        # 2. Use SmartDecomposer for everything else
        try:
            from tools.skynet_smart_decompose import SmartDecomposer
            decomposer = SmartDecomposer()
            # SmartDecomposer needs idle workers for assignment
            idle = self._get_idle_workers()
            subtasks = decomposer.decompose(prompt)
            # Re-map workers to idle ones (SmartDecomposer may use static list)
            for i, st in enumerate(subtasks):
                if st["worker"] not in idle and idle:
                    st["worker"] = idle[i % len(idle)]
            return subtasks
        except Exception as e:
            log(f"SmartDecomposer failed ({e}), using heuristic fallback", "WARN")

        # 3. Heuristic fallback (original logic)
        subtasks = []
        priority = 5
        if any(kw in prompt_lower for kw in ["urgent", "critical", "asap", "now", "immediately"]):
            priority = 1
        elif any(kw in prompt_lower for kw in ["fix", "bug", "error", "broken", "crash"]):
            priority = 3
            return subtasks

        # 2. Multiple directories or file paths → split by path
        paths = re.findall(r'(?:core/|tools/|Skynet/|tests/|ui/|docs/)\S*', prompt)
        if len(paths) >= 2:
            available = self._get_idle_workers()
            for i, path in enumerate(paths):
                worker = available[i % len(available)]
                subtasks.append({
                    "worker": worker,
                    "task": f"{prompt.split(paths[0])[0].strip()} {path}",
                    "priority": 5,
                })
            return subtasks

        # 3. Code review + test pattern
        if ("review" in prompt_lower or "audit" in prompt_lower) and ("test" in prompt_lower or "validate" in prompt_lower):
            available = self._get_idle_workers()
            review_part = re.sub(r'\b(and\s+)?(test|validate|verify|run tests)\b', '', prompt, flags=re.IGNORECASE).strip()
            test_part = f"Run tests and validate changes related to: {prompt}"
            subtasks.append({"worker": available[0], "task": review_part, "priority": 5})
            if len(available) >= 2:
                subtasks.append({"worker": available[1], "task": test_part, "priority": 4})
            return subtasks

        # 4. "scan" or "audit" with multiple areas
        if any(kw in prompt_lower for kw in ["scan", "audit", "check", "inspect"]):
            areas = re.findall(r'\b(endpoints?|stubs?|imports?|security|performance|files?|modules?|functions?)\b', prompt_lower)
            if len(areas) >= 2:
                available = self._get_idle_workers()
                for i, area in enumerate(set(areas)):
                    worker = available[i % len(available)]
                    subtasks.append({
                        "worker": worker,
                        "task": f"{prompt} — focus on: {area}",
                        "priority": 5,
                    })
                return subtasks

        # 5. Parallel keyword → broadcast to all idle workers
        if any(kw in prompt_lower for kw in ["all workers", "everyone", "broadcast"]):
            available = self._get_idle_workers()
            for worker in available:
                subtasks.append({"worker": worker, "task": prompt, "priority": 5})
            return subtasks

        # 6. Default: size-based routing — short→1 worker, medium→2, long→up to 4
        available = self._get_idle_workers()
        prompt_len = len(prompt)
        if prompt_len < 50:
            n_workers = 1
        elif prompt_len < 200:
            n_workers = min(2, len(available))
        else:
            n_workers = min(4, len(available))

        if n_workers == 1 or len(available) == 1:
            subtasks.append({"worker": available[0] if available else "alpha", "task": prompt, "priority": priority})
        else:
            for i in range(n_workers):
                subtasks.append({"worker": available[i], "task": prompt, "priority": priority})
        return subtasks

    def _get_idle_workers(self):
        """Return list of idle worker names, ranked by reliability score."""
        now = time.time()
        if self._idle_cache and (now - self._idle_cache_ts) < self._idle_cache_ttl:
            return self._idle_cache
        try:
            states = scan_all_states(self.workers)
            idle = [name for name, state in states.items() if state == "IDLE"]
            if idle:
                # Rank by scoring — best workers first
                try:
                    from tools.skynet_realtime import get_best_workers
                    ranked = get_best_workers(len(idle))
                    idle = [w for w in ranked if w in idle] + [w for w in idle if w not in ranked]
                except Exception:
                    pass
                self._idle_cache = idle
                self._idle_cache_ts = now
                return idle
        except Exception:
            pass
        fallback = self.worker_names[:len(self.workers)]
        self._idle_cache = fallback
        self._idle_cache_ts = now
        return fallback

    def dispatch_all(self, subtasks):
        """Dispatch all sub-tasks to their assigned workers.

        Returns dict of worker_name → True/False.
        """
        if len(subtasks) == 1:
            st = subtasks[0]
            log(f"Single dispatch → {st['worker'].upper()}", "SYS")
            ok = dispatch_to_worker(st["worker"], st["task"], self.workers, self.orch_hwnd)
            return {st["worker"]: ok}

        # Multiple subtasks → parallel dispatch
        tasks_by_worker = {st["worker"]: st["task"] for st in subtasks}
        log(f"Parallel dispatch → {list(tasks_by_worker.keys())}", "SYS")
        return dispatch_parallel(tasks_by_worker, self.workers, self.orch_hwnd)

    def collect_results(self, expected_workers, timeout=120, after_ts=None):
        """Poll bus for results from expected workers until all arrive or timeout.

        Only considers messages posted AFTER after_ts (ISO string) to avoid stale results.
        Returns dict of worker_name → result_content (or None if missing).
        """
        log(f"Collecting results from {expected_workers} (timeout={timeout}s)", "SYS")
        deadline = time.time() + timeout
        collected = {}
        seen_ids = set()

        # Snapshot existing message IDs to ignore stale results
        if after_ts is None:
            existing = bus_messages(limit=100)
            for m in existing:
                seen_ids.add(m.get("id", ""))
            log(f"Ignoring {len(seen_ids)} pre-existing bus messages", "SYS")

        while time.time() < deadline:
            messages = bus_messages(limit=100)
            for msg in messages:
                msg_id = msg.get("id", "")
                sender = msg.get("sender", "")
                msg_type = msg.get("type", "")

                if msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)
                if sender in expected_workers and msg_type == "result":
                    collected[sender] = msg.get("content", "")
                    log(f"Result from {sender.upper()}: {msg.get('content', '')[:80]}...", "OK")

            if all(w in collected for w in expected_workers):
                log(f"All {len(expected_workers)} workers reported", "OK")
                break

            missing = [w for w in expected_workers if w not in collected]
            remaining = int(deadline - time.time())
            if remaining > 0 and remaining % 30 == 0:
                log(f"Waiting for {missing} ({remaining}s left)", "SYS")
            time.sleep(3)

        missing = [w for w in expected_workers if w not in collected]
        if missing:
            log(f"Timeout: missing results from {missing}", "WARN")
            for w in missing:
                collected[w] = None

        return collected

    def synthesize(self, prompt, subtasks, results):
        """Combine results into a final synthesis report.

        results can be:
        - dict of worker → string (legacy bus-based)
        - dict of worker → {status, text, elapsed_s} (realtime collector)
        - dict of worker → None (missing)
        """
        lines = [f"# Orchestration Report", f"**Prompt:** {prompt}", f"**Subtasks:** {len(subtasks)}", ""]

        for st in subtasks:
            worker = st["worker"]
            raw = results.get(worker)

            # Normalize: handle both string and dict formats
            if isinstance(raw, dict):
                text = raw.get("text") or raw.get("content")
                status_str = raw.get("status", "unknown")
                elapsed = raw.get("elapsed_s", 0)
                status = "\u2705" if status_str == "complete" else f"\u274c {status_str.upper()}"
                if elapsed:
                    status += f" ({elapsed}s)"
            elif isinstance(raw, str) and raw:
                text = raw
                status = "\u2705"
            else:
                text = None
                status = "\u274c MISSING"

            lines.append(f"## {worker.upper()} {status}")
            lines.append(f"**Task:** {st['task'][:120]}")
            if text:
                lines.append(f"**Result:** {text[:500]}")
            lines.append("")

        success_count = sum(1 for r in results.values()
                          if r is not None and (not isinstance(r, dict) or r.get("status") == "complete"))
        lines.append(f"**Summary:** {success_count}/{len(subtasks)} workers reported results")
        return "\n".join(lines)

    def run(self, prompt, timeout=120, realtime=True, auto_retry=True):
        """Full pipeline: guard → decompose → snapshot → dispatch → collect → retry → synthesize.

        v3 pipeline with identity guard:
        - Identity guard rejects worker preambles at orchestrator entry
        - Conversation fingerprinting (snapshot before dispatch, extract only NEW items)
        - Auto-retry: stale/timeout workers get re-dispatched to idle workers
        - Worker scoring: records outcomes for smart future routing
        - Adaptive polling: 0.5s for first 5s, then 2s
        """
        t0 = time.time()

        # Identity Guard: reject worker preambles at orchestrator entry
        guard = get_orchestrator_guard()
        safe, reason = guard.validate(prompt)
        if not safe:
            log(f"IDENTITY GUARD BLOCKED: {reason}", "ERR")
            bus_post("orchestrator", "security", "blocked", f"Rejected: {reason}")
            return {"success": False, "error": f"Identity guard: {reason}", "elapsed_ms": 0}

        log(f"Orchestrating: {prompt[:100]}", "SYS")

        # Step 0: Pre-flight — recover any UNKNOWN workers
        states = scan_all_states(self.workers)
        for name, state in states.items():
            if state == "UNKNOWN":
                log(f"Pre-flight: {name.upper()} is UNKNOWN — recovering", "WARN")
                recover_worker(name, self.workers, self.orch_hwnd)

        # Step 1: Decompose (uses worker scoring for smart routing)
        self._idle_cache = None
        subtasks = self.decompose_task(prompt)
        log(f"Decomposed into {len(subtasks)} subtask(s):", "OK")
        for st in subtasks:
            log(f"  → {st['worker'].upper()} [P{st.get('priority', 5)}]: {st['task'][:80]}", "SYS")

        dispatched_workers = [st["worker"] for st in subtasks]

        # Step 2: Snapshot conversation baselines (v3 fingerprinting)
        collector = RealtimeCollector(poll_interval=2.0)
        if realtime:
            log("Snapshotting conversation baselines...", "SYS")
            collector.snapshot_baselines(dispatched_workers)

        # Step 3: Dispatch
        dispatch_results = self.dispatch_all(subtasks)
        dispatched = [name for name, ok in dispatch_results.items() if ok]
        failed = [name for name, ok in dispatch_results.items() if not ok]
        if failed:
            log(f"Dispatch failed for: {failed}", "WARN")
        if not dispatched:
            log("No workers received tasks", "ERR")
            return {"success": False, "error": "All dispatches failed", "elapsed_ms": (time.time() - t0) * 1000}

        # Step 4: Collect results with fingerprinting + optional auto-retry
        if realtime:
            task_map = {st["worker"]: st["task"] for st in subtasks}
            task_types = {st["worker"]: st.get("type", "general") for st in subtasks}

            if auto_retry:
                log("Collecting via REAL-TIME UIA + auto-retry...", "SYS")

                def _retry_dispatch(worker_name, task_text):
                    dispatch_to_worker(worker_name, task_text, self.workers, self.orch_hwnd)

                results = collector.collect_with_retry(
                    dispatched, task_map, timeout=timeout,
                    max_retries=1, dispatch_fn=_retry_dispatch,
                )
            else:
                log("Collecting via REAL-TIME UIA...", "SYS")
                results = collector.collect(dispatched, timeout=timeout, task_types=task_types)
        else:
            log("Collecting via bus polling (legacy)...", "SYS")
            results = self.collect_results(dispatched, timeout=timeout)

        # Step 4: Synthesize
        report = self.synthesize(prompt, subtasks, results)
        elapsed_ms = (time.time() - t0) * 1000

        # Record metrics
        try:
            from tools.skynet_metrics import SkynetMetrics
            m = SkynetMetrics()
            if realtime:
                success_count = sum(1 for r in results.values()
                                   if isinstance(r, dict) and r.get("status") == "complete")
            else:
                success_count = sum(1 for r in results.values() if r is not None)
            m.record_e2e_task(
                f"orch_{int(time.time())}",
                dispatched,
                elapsed_ms,
                success_count,
                len(dispatched) - success_count,
            )
        except Exception:
            pass

        # Post synthesis to bus
        bus_post("orchestrator", "orchestrator", "synthesis", report[:500])

        log(f"Orchestration complete in {elapsed_ms:.0f}ms", "OK")
        return {
            "success": True,
            "report": report,
            "subtasks": subtasks,
            "results": results,
            "dispatched": dispatched,
            "elapsed_ms": elapsed_ms,
        }

    def convene(self, topic, context, n_workers=2, timeout=120):
        """Initiate a convene session — multiple workers coordinate on a sub-problem."""
        log(f"Convening {n_workers} workers on: {topic}", "SYS")

        # Post convene request to Go server
        try:
            body = json.dumps({
                "initiator": "orchestrator",
                "topic": topic,
                "context": context,
                "need_workers": n_workers,
            }).encode()
            req = Request(f"{SKYNET_URL}/bus/convene", data=body, headers={"Content-Type": "application/json"}, method="POST")
            resp = json.loads(urlopen(req, timeout=5).read())
            session_id = resp.get("session_id", "unknown")
            log(f"Convene session created: {session_id}", "OK")
        except Exception as e:
            log(f"Convene server endpoint failed (continuing with direct dispatch): {e}", "WARN")
            session_id = f"local_{int(time.time())}"

        # Dispatch to n idle workers with convene context
        available = self._get_idle_workers()[:n_workers]
        task_text = f"CONVENE SESSION [{session_id}]: {topic}\nContext: {context}\nCoordinate with other workers via bus topic='convene'."
        tasks_by_worker = {w: task_text for w in available}
        results = dispatch_parallel(tasks_by_worker, self.workers, self.orch_hwnd)
        dispatched = [w for w, ok in results.items() if ok]

        if not dispatched:
            return {"success": False, "error": "No workers available for convene"}

        collected = self.collect_results(dispatched, timeout=timeout)
        report = self.synthesize(f"Convene: {topic}", [{"worker": w, "task": task_text} for w in dispatched], collected)
        return {"success": True, "session_id": session_id, "report": report, "results": collected}

    def reactive_run(self, prompt, timeout=180):
        """Reactive pipeline — UIA real-time + bus monitoring for help/convene requests."""
        t0 = time.time()
        log(f"Reactive orchestrating: {prompt[:100]}", "SYS")

        # Pre-flight recovery
        states = scan_all_states(self.workers)
        for name, state in states.items():
            if state == "UNKNOWN":
                log(f"Pre-flight: {name.upper()} is UNKNOWN — recovering", "WARN")
                recover_worker(name, self.workers, self.orch_hwnd)

        self._idle_cache = None
        subtasks = self.decompose_task(prompt)
        log(f"Decomposed into {len(subtasks)} subtask(s)", "OK")
        for st in subtasks:
            log(f"  → {st['worker'].upper()} [P{st.get('priority', 5)}]: {st['task'][:80]}", "SYS")

        dispatch_results = self.dispatch_all(subtasks)
        dispatched = [name for name, ok in dispatch_results.items() if ok]
        if not dispatched:
            return {"success": False, "error": "All dispatches failed"}

        # Dual collection: UIA real-time + bus watch for help/convene
        collector = RealtimeCollector(poll_interval=2.0)
        collected = collector.collect(dispatched, timeout=timeout)

        # Also check bus for any help/convene requests that came in during collection
        try:
            messages = bus_messages(limit=50)
            for msg in messages:
                msg_type = msg.get("type", "").lower()
                sender = msg.get("sender", "")
                if msg_type in ("request", "help"):
                    log(f"Help request from {sender}: {msg.get('content', '')[:60]}", "WARN")
                    idle = self._get_idle_workers()
                    free = [w for w in idle if w not in dispatched]
                    if free:
                        helper_task = f"Help {sender} with: {msg.get('content', '')}"
                        dispatch_to_worker(free[0], helper_task, self.workers, self.orch_hwnd)
                        log(f"Routed help to {free[0].upper()}", "OK")
        except Exception:
            pass

        report = self.synthesize(prompt, subtasks, collected)
        elapsed_ms = (time.time() - t0) * 1000
        bus_post("orchestrator", "orchestrator", "synthesis", report[:500])
        log(f"Reactive orchestration complete in {elapsed_ms:.0f}ms", "OK")
        return {
            "success": True,
            "report": report,
            "subtasks": subtasks,
            "results": collected,
            "dispatched": dispatched,
            "elapsed_ms": elapsed_ms,
        }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Skynet Master Orchestrator")
    parser.add_argument("--prompt", type=str, required=True, help="Task prompt to orchestrate")
    parser.add_argument("--decompose-only", action="store_true", help="Show decomposition without dispatching")
    parser.add_argument("--timeout", type=int, default=120, help="Result collection timeout (seconds)")
    parser.add_argument("--reactive", action="store_true", help="Reactive mode: UIA real-time + bus monitoring")
    parser.add_argument("--legacy-bus", action="store_true", help="Use legacy bus polling instead of real-time UIA")
    args = parser.parse_args()

    orch = SkynetOrchestrator()

    if args.decompose_only:
        subtasks = orch.decompose_task(args.prompt)
        print(json.dumps(subtasks, indent=2))
    elif args.reactive:
        result = orch.reactive_run(args.prompt, timeout=args.timeout)
        if result.get("report"):
            print(result["report"])
        else:
            print(json.dumps(result, indent=2))
    else:
        result = orch.run(args.prompt, timeout=args.timeout, realtime=not args.legacy_bus)
        if result.get("report"):
            print(result["report"])
        else:
            print(json.dumps(result, indent=2))
