#!/usr/bin/env python3
"""skynet_cli.py — Unified CLI for all Skynet operations.

Usage:
    python tools/skynet_cli.py dispatch --worker alpha --task "count files in core/"
    python tools/skynet_cli.py health
    python tools/skynet_cli.py audit [--json]
    python tools/skynet_cli.py decompose --prompt "build X and test Y"
    python tools/skynet_cli.py status
    python tools/skynet_cli.py pipeline --steps '[["step1"],["step2"]]'
    python tools/skynet_cli.py metrics [--summary | --export-csv out.csv]
    python tools/skynet_cli.py bus [--limit 10] [--topic alpha]
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _bus_post(content):
    """Post result to Skynet bus via SpamGuard (best-effort)."""
    bus_msg = {"sender": "cli", "topic": "orchestrator", "type": "result", "content": content}
    try:
        from tools.skynet_spam_guard import guarded_publish
        guarded_publish(bus_msg)
    except ImportError:
        # Fallback: raw urllib when SpamGuard unavailable
        from urllib.request import urlopen, Request
        body = json.dumps(bus_msg).encode()
        try:
            urlopen(Request("http://localhost:8420/bus/publish", data=body, headers={"Content-Type": "application/json"}), timeout=3)
        except Exception:
            pass
    # signed: beta


def _bus_get(limit=20, topic=None):
    """Read messages from Skynet bus."""
    from urllib.request import urlopen
    url = f"http://localhost:8420/bus/messages?limit={limit}"
    if topic:
        url += f"&topic={topic}"
    try:
        return json.loads(urlopen(url, timeout=5).read())
    except Exception as e:
        print(f"Error reading bus: {e}", file=sys.stderr)
        return []


# ── Subcommand handlers ─────────────────────────────────────────────────────

def cmd_dispatch(args):
    """Dispatch a task to a worker."""
    from tools.skynet_dispatch import dispatch_to_worker, dispatch_parallel, load_workers, load_orch_hwnd

    if args.worker == "auto":
        from tools.skynet_dispatch import smart_dispatch
        result = smart_dispatch(args.task)
        print(f"Smart dispatch result: {result}")
        return 0 if result else 1

    if args.parallel:
        # --parallel expects JSON: {"alpha": "task1", "beta": "task2"}
        try:
            tasks = json.loads(args.task)
        except json.JSONDecodeError:
            print("Error: --parallel requires JSON dict as --task, e.g. '{\"alpha\":\"task1\",\"beta\":\"task2\"}'", file=sys.stderr)
            return 1
        results = dispatch_parallel(tasks)
        for w, ok in results.items():
            print(f"  {w}: {'OK' if ok else 'FAIL'}")
        return 0 if all(results.values()) else 1

    ok = dispatch_to_worker(args.worker, args.task)
    return 0 if ok else 1


def cmd_health(args):
    """Run the health dashboard."""
    from tools.skynet_health import SkynetHealthDashboard
    dash = SkynetHealthDashboard()
    report = dash.run()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        for k, v in report.items():
            print(f"  {k}: {v}")
    return 0


def cmd_audit(args):
    """Run the Skynet auditor."""
    from tools.skynet_audit import SkynetAuditor, print_scorecard
    auditor = SkynetAuditor()
    results = auditor.run_all()
    if args.json:
        out = {"checks": [{"name": r.name, "passed": r.passed, "detail": r.detail} for r in results]}
        out["health_score"] = int(sum(1 for r in results if r.passed) / max(len(results), 1) * 100)
        print(json.dumps(out, indent=2))
    else:
        print_scorecard(results)
    return 0 if all(r.passed for r in results) else 1


def cmd_decompose(args):
    """Decompose a prompt into worker-sized subtasks."""
    from tools.skynet_orchestrate import SkynetOrchestrator
    orch = SkynetOrchestrator()
    subtasks = orch.decompose_task(args.prompt)
    for i, st in enumerate(subtasks, 1):
        print(f"  [{i}] worker={st.get('worker', '?'):6s}  prio={st.get('priority', '?')}  task={st.get('task', '')[:90]}")
    if args.json:
        print(json.dumps(subtasks, indent=2))
    return 0


def cmd_status(args):
    """Show worker states, scores, and bus depth."""
    from tools.skynet_dispatch import load_workers, scan_all_states, load_orch_hwnd

    workers = load_workers()
    if not workers:
        print("No workers found in data/workers.json")
        return 1

    orch_hwnd = load_orch_hwnd()
    hwnds = {w["name"]: w["hwnd"] for w in workers}
    if orch_hwnd:
        hwnds["orchestrator"] = orch_hwnd

    states = scan_all_states(hwnds)

    # Worker scores
    scores = {}
    scores_file = ROOT / "data" / "worker_scores.json"
    if scores_file.exists():
        try:
            scores = json.loads(scores_file.read_text())
        except Exception:
            pass

    print(f"\n  {'Worker':<14} {'HWND':<10} {'State':<12} {'Score':<8}")
    print(f"  {'─'*14} {'─'*10} {'─'*12} {'─'*8}")
    for name, hwnd in hwnds.items():
        state = states.get(name, "UNKNOWN")
        score = scores.get(name, {}).get("score", "—")
        tag = "⚡" if name == "orchestrator" else "🤖"
        print(f"  {tag} {name:<12} {hwnd:<10} {state:<12} {score}")

    # Bus depth
    bus = _bus_get(limit=1)
    if isinstance(bus, dict):
        depth = bus.get("bus_depth", "?")
    elif isinstance(bus, list) and bus:
        depth = len(bus)
    else:
        depth = "?"
    print(f"\n  Bus depth: {depth}")

    if args.json:
        print(json.dumps({"workers": {n: {"hwnd": h, "state": states.get(n, "UNKNOWN")} for n, h in hwnds.items()}, "scores": scores}, indent=2))
    return 0


def cmd_pipeline(args):
    """Run a composable pipeline."""
    from tools.skynet_pipeline import SkynetPipeline
    pipe = SkynetPipeline()

    try:
        steps = json.loads(args.steps)
    except json.JSONDecodeError:
        print("Error: --steps must be valid JSON", file=sys.stderr)
        return 1

    if isinstance(steps, list):
        result = pipe.chain(steps)
    elif isinstance(steps, dict):
        result = pipe.parallel(steps)
    else:
        print("Error: --steps must be a JSON list (chain) or dict (parallel)", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_metrics(args):
    """Manage research metrics."""
    from tools.skynet_metrics import SkynetMetrics
    m = SkynetMetrics()

    if args.export_csv:
        from tools.skynet_metrics import export_csv
        export_csv(args.export_csv)
    elif args.run_benchmarks:
        m.run_all_benchmarks()
    else:
        summary = m.generate_summary()
        print(json.dumps(summary, indent=2))
    return 0


def cmd_bus(args):
    """Read or post to the Skynet message bus."""
    if args.post:
        _bus_post(args.post)
        print("Posted to bus.")
        return 0

    messages = _bus_get(limit=args.limit, topic=args.topic)
    if isinstance(messages, dict) and "messages" in messages:
        messages = messages["messages"]

    if not messages:
        print("No messages.")
        return 0

    for msg in messages:
        ts = msg.get("ts", msg.get("timestamp", ""))
        sender = msg.get("sender", "?")
        topic = msg.get("topic", "?")
        mtype = msg.get("type", "?")
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) > 120:
            content = content[:117] + "..."
        print(f"  [{ts}] {sender:>8} → {topic:<14} [{mtype}] {content}")
    return 0


# ── Main ─────────────────────────────────────────────────────────────────────

def _add_core_subparsers(sub):
    """Register core command subparsers (dispatch, health, audit, decompose)."""
    p_dispatch = sub.add_parser("dispatch", help="Dispatch task to worker(s)")
    p_dispatch.add_argument("--worker", "-w", default="auto", help="Worker name or 'auto' for smart dispatch")
    p_dispatch.add_argument("--task", "-t", required=True, help="Task string (or JSON dict for --parallel)")
    p_dispatch.add_argument("--parallel", "-p", action="store_true", help="Parallel dispatch (--task must be JSON dict)")
    p_dispatch.set_defaults(func=cmd_dispatch)

    p_health = sub.add_parser("health", help="Run health dashboard")
    p_health.add_argument("--json", action="store_true")
    p_health.set_defaults(func=cmd_health)

    p_audit = sub.add_parser("audit", help="Run Skynet auditor")
    p_audit.add_argument("--json", action="store_true")
    p_audit.set_defaults(func=cmd_audit)

    p_decompose = sub.add_parser("decompose", help="Decompose prompt into subtasks")
    p_decompose.add_argument("--prompt", "-p", required=True)
    p_decompose.add_argument("--json", action="store_true")
    p_decompose.set_defaults(func=cmd_decompose)


def _add_ops_subparsers(sub):
    """Register operational command subparsers (status, pipeline, metrics, bus)."""
    p_status = sub.add_parser("status", help="Show worker states and scores")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_pipeline = sub.add_parser("pipeline", help="Run composable pipeline")
    p_pipeline.add_argument("--steps", "-s", required=True, help="JSON list (chain) or dict (parallel)")
    p_pipeline.set_defaults(func=cmd_pipeline)

    p_metrics = sub.add_parser("metrics", help="Research metrics")
    p_metrics.add_argument("--export-csv", type=str, help="Export to CSV file")
    p_metrics.add_argument("--run-benchmarks", action="store_true")
    p_metrics.set_defaults(func=cmd_metrics)

    p_bus = sub.add_parser("bus", help="Read/post to message bus")
    p_bus.add_argument("--limit", "-l", type=int, default=20)
    p_bus.add_argument("--topic", "-t", type=str)
    p_bus.add_argument("--post", type=str, help="Post a message to the bus")
    p_bus.set_defaults(func=cmd_bus)


def _build_cli_parser():
    """Build the CLI argument parser with all subparsers."""
    parser = argparse.ArgumentParser(
        prog="skynet",
        description="Unified CLI for Skynet multi-agent system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  skynet dispatch --worker alpha --task 'count files'\n"
               "  skynet status --json\n"
               "  skynet audit\n"
               "  skynet bus --limit 5 --topic alpha\n",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    _add_core_subparsers(sub)
    _add_ops_subparsers(sub)
    return parser


def main():
    parser = _build_cli_parser()
    args = parser.parse_args()

    try:
        rc = args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        rc = 130
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        rc = 1

    sys.exit(rc)


if __name__ == "__main__":
    main()
