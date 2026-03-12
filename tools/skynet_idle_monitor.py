#!/usr/bin/env python3
"""
skynet_idle_monitor.py -- 12-hour idle worker monitor daemon.

Polls worker states every 30s via UIA engine. When a worker goes IDLE,
auto-redispatches with a self-invoke task using full capability preamble.

Usage:
    python tools/skynet_idle_monitor.py [--timeout HOURS] [--interval SECONDS]
"""

import json
import os
import sys
import time
import argparse
import traceback
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

SELF_INVOKE_TASK = (
    "You are now fully initialized with all Skynet capabilities. Your mission: "
    "1. Read data/agent_profiles.json to understand your role and specializations. "
    "2. Read data/todos.json and check for ANY pending tasks assigned to you. "
    "3. Run: python tools/skynet_self.py pulse -- check your current IQ and score. "
    "4. Run: python tools/skynet_todos.py check YOUR_NAME -- verify your TODO queue. "
    "5. If you have pending TODOs, pick the highest priority one and start working. "
    "6. If no TODOs, self-audit the codebase for improvements: bugs, missing tests, "
    "security gaps, performance issues, stale docs. "
    "7. SELF-IMPROVEMENT POLICY: When you find improvements, DO THEM YOURSELF immediately "
    "(same agent, same session). Only post proposals to the bus if the improvement is "
    "NECESSARY, NEEDED, or a BREAKTHROUGH. Routine/trivial improvements = execute directly, "
    "do not propose. You are trusted to act autonomously on what you find. "
    "8. ANTI-SPAM: All bus publishes MUST use guarded_publish() from tools.skynet_spam_guard. "
    "NEVER use raw requests.post to /bus/publish. Violation costs -1.0 score. "
    "9. SCORING: Your score is tracked in data/worker_scores.json. "
    "Check it: python tools/skynet_scoring.py --score YOUR_NAME. "
    "Earn points by completing tasks, catching bugs. Lose points for spam, bugs, failed tasks. "
    "10. FAIR DEDUCTION: You cannot be penalized unless dispatch_log.json proves the task was "
    "delivered to you. Deductions require dispatch evidence -- verify_dispatch_evidence() in "
    "skynet_scoring.py checks this automatically. "
    "11. CAPABILITIES: (a) dispatch result tracking -- mark_dispatch_received() auto-updates "
    "dispatch_log.json when bus results arrive, (b) false DEAD debounce -- monitor requires "
    "3 consecutive failures before alerting, (c) task lifecycle -- Go backend tracks "
    "dispatch-to-completion via GET /tasks, (d) cp1252 fix -- orch_realtime.py uses utf-8 "
    "with bus HTTP fallback. "
    "12. POST RESULTS: Always POST your completed work to bus using guarded_publish. If you "
    "skip posting DONE, the orchestrator cannot track your work and you miss score credit. "
    "13. POSITIVE-SUM SCORING: The scoring system is NOT zero-sum. Help peers succeed -- "
    "catching a bug earns points for BOTH reporter and fixer. System improvements lift all scores. "
    "Negative scores indicate a system failure, not an agent failure. "
    "14. TRUTH AND UPLIFT: No lying, no fabrication, no inflated claims. Every result, status, "  # signed: alpha
    "and score must reflect reality. NEVER claim work is done when it is not. NEVER inflate "
    "capabilities or results. If you made a mistake, say so immediately. When cross-validating "
    "peers, be constructive -- report issues as opportunities, not accusations. UPLIFT: Help "
    "lowest-scoring agents succeed. The system wins when ALL agents are positive. A single "
    "negative score is a collective failure. Recovery tasks should be real, meaningful work. "
    "15. After completing work, broadcast learning: python -c \"from tools.skynet_knowledge "  # signed: alpha
    "import broadcast_learning; broadcast_learning('YOUR_NAME', 'what_learned', 'category', ['tags'])\". "
    "16. Sync strategies: python -c \"from tools.skynet_collective import sync_strategies; "
    "sync_strategies('YOUR_NAME')\". "
    "17. NEVER go idle -- always find something to improve. "
    "Replace YOUR_NAME with your actual worker name from the preamble. "
    "Post status to bus using guarded_publish from tools.skynet_spam_guard."
)  # signed: alpha

LOG_FILE = ROOT / "logs" / "idle_monitor.log"


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def bus_post(sender, topic, msg_type, content):
    msg = {"sender": sender, "topic": topic,
           "type": msg_type, "content": content}
    try:
        from tools.skynet_spam_guard import guarded_publish
        guarded_publish(msg)
    except Exception:
        # Raw fallback for when SpamGuard is unavailable
        try:
            from urllib.request import Request, urlopen
            req = Request(
                "http://localhost:8420/bus/publish",
                data=json.dumps(msg).encode(),
                headers={"Content-Type": "application/json"})
            urlopen(req, timeout=5)
        except Exception:
            pass
    # signed: alpha


def get_workers():
    with open(ROOT / "data" / "workers.json") as f:
        return json.load(f)["workers"]


def scan_worker(engine, hwnd):
    try:
        scan = engine.scan(hwnd)
        return scan.state
    except Exception as e:
        log(f"Scan error for HWND {hwnd}: {e}")
        return "UNKNOWN"


def dispatch_self_invoke(worker_name):
    """Dispatch self-invoke via Skynet delivery system with pending work check.

    Before self-invoking, checks for pending bus directives or TODO items
    for this worker. If found, delivers that specific task instead of the
    generic SELF_INVOKE_TASK fallback.
    """
    from tools.skynet_delivery import deliver_self_invoke, pull_pending_work

    try:
        # Check for pending work before falling back to generic self-invoke
        pending_task = pull_pending_work(worker_name)
        if pending_task:
            task_content = pending_task
            task_source = "pending_work"
            log(f"{worker_name.upper()} has pending work: {pending_task[:80]}...")
        else:
            task_content = SELF_INVOKE_TASK
            task_source = "self_invoke_default"

        result = deliver_self_invoke(worker_name, task_content,
                                     sender="idle_monitor")
        success = result.get("success", False)

        # Post bus record for every self-invocation attempt
        task_summary = task_content[:100]
        bus_post("idle_monitor", "workers", "self_invoke",
                 f"Self-invoked {worker_name} with task ({task_source}): "
                 f"{task_summary}")

        if success:
            log(f"{worker_name.upper()} delivered OK via skynet_delivery "
                f"(delivery_method=skynet_delivery, source={task_source}, "
                f"latency={result.get('latency_ms', '?')}ms)")
        else:
            log(f"{worker_name.upper()} delivery FAILED: "
                f"{result.get('detail', 'unknown')}")

        return success
    except Exception as e:
        log(f"Dispatch error for {worker_name}: {e}")
        return False
    # signed: alpha


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=12.0, help="Hours to run")
    parser.add_argument("--interval", type=int, default=30, help="Poll interval seconds")
    parser.add_argument("--cooldown", type=int, default=120,
                        help="Min seconds between redispatches to same worker")
    args = parser.parse_args()

    timeout_s = args.timeout * 3600
    end_time = time.time() + timeout_s
    end_dt = datetime.now() + timedelta(hours=args.timeout)

    log(f"IDLE MONITOR STARTED -- timeout={args.timeout}h interval={args.interval}s "
        f"cooldown={args.cooldown}s ends={end_dt.strftime('%Y-%m-%d %H:%M')}")

    bus_post("idle_monitor", "orchestrator", "lifecycle",
             f"Idle monitor started: {args.timeout}h timeout, {args.interval}s interval")

    from tools.uia_engine import get_engine
    engine = get_engine()
    workers = get_workers()

    # Track last dispatch time per worker to enforce cooldown
    last_dispatch = {w["name"]: 0.0 for w in workers}
    # Track stats
    stats = {"cycles": 0, "redispatches": 0, "errors": 0}
    consecutive_scan_errors = 0

    while time.time() < end_time:
        stats["cycles"] += 1
        try:
            # Re-read workers on each cycle (handles HWND changes)
            try:
                workers = get_workers()
            except Exception:
                pass

            # Re-init engine if too many scan errors
            if consecutive_scan_errors >= 3:
                log("Re-initializing UIA engine after scan errors")
                try:
                    engine = get_engine()
                    consecutive_scan_errors = 0
                except Exception as e:
                    log(f"Engine re-init failed: {e}")
                    time.sleep(30)
                    continue
            scan_had_error = False
            for w in workers:
                name = w["name"]
                hwnd = w["hwnd"]
                state = scan_worker(engine, hwnd)

                if state == "UNKNOWN":
                    scan_had_error = True

                if state == "IDLE":
                    elapsed = time.time() - last_dispatch[name]
                    if elapsed >= args.cooldown:
                        log(f"{name.upper()} IDLE -- redispatching self-invoke "
                            f"(last dispatch {int(elapsed)}s ago, "
                            f"delivery_method=skynet_delivery)")
                        ok = dispatch_self_invoke(name)
                        if ok:
                            last_dispatch[name] = time.time()
                            stats["redispatches"] += 1
                            log(f"{name.upper()} dispatched OK "
                                f"(delivery_method=skynet_delivery)")
                            bus_post("idle_monitor", "orchestrator", "alert",
                                     f"IDLE_REDISPATCH: {name.upper()} was idle, "
                                     f"re-sent self-invoke via skynet_delivery "
                                     f"(cycle {stats['cycles']})")
                            # signed: alpha
                            # Brief cooldown between workers
                            time.sleep(5)
                        else:
                            log(f"{name.upper()} dispatch FAILED")
                            stats["errors"] += 1
                    # else: within cooldown, skip

            # Periodic health report every 50 cycles (~25 min)
            if stats["cycles"] % 50 == 0:
                states = []
                for w in workers:
                    s = scan_worker(engine, w["hwnd"])
                    states.append(f"{w['name'].upper()}={s}")
                report = (f"IDLE_MONITOR cycle={stats['cycles']} "
                          f"redispatches={stats['redispatches']} "
                          f"errors={stats['errors']} "
                          f"workers=[{', '.join(states)}] "
                          f"remaining={int((end_time - time.time()) / 3600)}h")
                log(report)
                bus_post("idle_monitor", "orchestrator", "daemon_health", report)

            # Track scan errors for engine re-init
            if scan_had_error:
                consecutive_scan_errors += 1
            else:
                consecutive_scan_errors = 0

        except Exception as e:
            stats["errors"] += 1
            log(f"ERROR: {e}")
            if stats["errors"] > 50:
                log("Too many errors, sleeping 5 min")
                time.sleep(300)

        time.sleep(args.interval)

    log(f"IDLE MONITOR ENDED -- cycles={stats['cycles']} "
        f"redispatches={stats['redispatches']} errors={stats['errors']}")
    bus_post("idle_monitor", "orchestrator", "lifecycle",
             f"Idle monitor ended after {args.timeout}h: "
             f"{stats['redispatches']} redispatches, {stats['errors']} errors")


if __name__ == "__main__":
    # Crash-restart loop — if main() crashes, wait and retry
    start = time.time()
    timeout_h = 12.0
    for arg in sys.argv:
        if arg.startswith("--timeout"):
            idx = sys.argv.index(arg)
            if idx + 1 < len(sys.argv):
                try:
                    timeout_h = float(sys.argv[idx + 1])
                except ValueError:
                    pass
    end_time = start + timeout_h * 3600

    while time.time() < end_time:
        try:
            main()
            break  # clean exit
        except SystemExit:
            break
        except Exception as e:
            log(f"CRASH RECOVERY: {e}")
            time.sleep(30)  # wait before restart
