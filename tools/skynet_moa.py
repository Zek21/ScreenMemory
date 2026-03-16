"""Mixture of Agents (MoA) dispatch for Skynet.
# signed: alpha

Multi-perspective task execution: sends the same task to N workers with
different personas (analyzer, implementer, critic, etc.), collects all
responses, then dispatches a synthesis task to an aggregator worker that
combines the best elements from each perspective.

This produces higher-quality results than single-worker dispatch by
leveraging cognitive diversity across agents.

Usage:
    python tools/skynet_moa.py "Fix auth middleware" --capabilities security
    python tools/skynet_moa.py "Optimize hot loop" --n 4 --timeout 180
    python tools/skynet_moa.py "Design caching layer" --dry-run
"""
# signed: alpha

import json
import os
import sys
import time
import hashlib
import argparse
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DATA_DIR = REPO_ROOT / "data"
MOA_STATE_PATH = DATA_DIR / "moa_state.json"
BUS_URL = "http://localhost:8420"

WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

# ── Persona definitions ──────────────────────────────────────────
# Each persona gets a preamble that frames how the worker should approach
# the task.  The diversity of perspectives is what makes MoA effective.

PERSONAS = [
    {
        "id": "analyzer",
        "label": "Analyzer",
        "preamble": (
            "ROLE: You are the ANALYZER. Your job is to deeply understand the problem "
            "before proposing solutions. Read all relevant code, trace call paths, "
            "identify root causes, and document your findings with file paths and line "
            "numbers. Focus on WHAT is happening and WHY, not on writing code yet."
        ),
    },
    {
        "id": "implementer",
        "label": "Implementer",
        "preamble": (
            "ROLE: You are the IMPLEMENTER. Your job is to write the actual code "
            "changes. Be precise, surgical, and complete. Every edit must compile "
            "and pass existing tests. Include file paths, exact old/new strings, "
            "and verify with py_compile or go build. Focus on correctness and "
            "completeness over analysis."
        ),
    },
    {
        "id": "critic",
        "label": "Critic",
        "preamble": (
            "ROLE: You are the CRITIC. Your job is to find flaws, edge cases, "
            "security issues, and potential regressions. Challenge assumptions. "
            "Check error handling, concurrency safety, backwards compatibility, "
            "and performance implications. If the approach is wrong, say so clearly "
            "with evidence."
        ),
    },
    {
        "id": "architect",
        "label": "Architect",
        "preamble": (
            "ROLE: You are the ARCHITECT. Your job is to evaluate the design and "
            "ensure it fits the broader system. Check for coupling, API consistency, "
            "separation of concerns, and future extensibility. Propose the cleanest "
            "interface even if it requires more refactoring. Think about maintainability."
        ),
    },
]

SYNTHESIS_PREAMBLE = (
    "ROLE: You are the SYNTHESIZER. You have received responses from multiple "
    "perspectives on the same task. Your job is to produce the BEST POSSIBLE "
    "final result by combining insights from all perspectives:\n"
    "- Use the Analyzer's understanding of the problem\n"
    "- Use the Implementer's code changes (verify they address the Analyzer's findings)\n"
    "- Apply the Critic's corrections and edge case handling\n"
    "- Respect the Architect's design guidance\n\n"
    "Produce a single, coherent, complete solution. If perspectives conflict, "
    "explain your reasoning for which approach wins. The final output must be "
    "implementable as-is — no TODOs, no placeholders."
)


def _generate_moa_id(task: str) -> str:
    """Generate a unique MoA session ID."""
    raw = f"moa:{task}:{time.time()}"
    return "moa_" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def _load_moa_state() -> dict:
    """Load MoA state from disk."""
    if MOA_STATE_PATH.exists():
        try:
            with open(MOA_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"sessions": {}, "history": [], "version": 1}


def _save_moa_state(state: dict) -> None:
    """Atomically persist MoA state."""
    MOA_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(MOA_STATE_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(MOA_STATE_PATH))
    # signed: alpha


def _get_idle_workers() -> List[str]:
    """Get list of currently idle workers from realtime.json or /status."""
    rt_path = DATA_DIR / "realtime.json"
    if rt_path.exists():
        try:
            with open(rt_path, "r", encoding="utf-8") as f:
                rt = json.load(f)
            idle = []
            for name in WORKER_NAMES:
                w = rt.get("workers", {}).get(name, {})
                if w.get("status", "IDLE").upper() in ("IDLE",):
                    idle.append(name)
            return idle
        except (json.JSONDecodeError, OSError):
            pass

    # Fallback: backend /status
    try:
        import urllib.request
        resp = urllib.request.urlopen(f"{BUS_URL}/status", timeout=3)
        data = json.loads(resp.read().decode())
        resp.close()
        idle = []
        for a in data.get("agents", []):
            if a.get("status", "IDLE").upper() in ("IDLE",):
                idle.append(a.get("name", "").lower())
        return [w for w in idle if w in WORKER_NAMES]
    except Exception:
        return list(WORKER_NAMES)  # assume all idle if we can't check
    # signed: alpha


def _poll_bus_for_results(
    moa_id: str, expected_workers: List[str], timeout: float = 120.0
) -> Dict[str, str]:
    """Poll bus for MoA responses from expected workers.

    Looks for messages with type=result and content containing the moa_id.

    Returns:
        Dict mapping worker_name -> response content.
    """
    import urllib.request

    results: Dict[str, str] = {}
    deadline = time.time() + timeout
    poll_interval = 2.0

    while time.time() < deadline and len(results) < len(expected_workers):
        try:
            resp = urllib.request.urlopen(
                f"{BUS_URL}/bus/messages?limit=50", timeout=5
            )
            messages = json.loads(resp.read().decode())
            resp.close()

            for msg in messages:
                sender = msg.get("sender", "").lower()
                content = msg.get("content", "")
                msg_type = msg.get("type", "")

                if (
                    sender in expected_workers
                    and sender not in results
                    and msg_type == "result"
                    and moa_id in content
                ):
                    results[sender] = content
        except Exception:
            pass

        if len(results) < len(expected_workers):
            time.sleep(poll_interval)

    return results
    # signed: alpha


class MoADispatch:
    """Mixture of Agents dispatcher.

    Orchestrates multi-perspective task execution:
    1. Assigns personas to available workers
    2. Dispatches task with persona-specific preambles
    3. Collects responses from all workers
    4. Synthesizes a final result from all perspectives
    # signed: alpha
    """

    def __init__(self):
        self._state = _load_moa_state()

    def dispatch_moa(
        self,
        task: str,
        n_workers: int = 3,
        timeout: float = 120.0,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Execute full MoA cycle: dispatch → collect → synthesize.

        Args:
            task: The task description to send to all workers.
            n_workers: Number of workers to use (1-4, capped by idle count).
            timeout: Seconds to wait for worker responses.
            dry_run: If True, show plan without dispatching.

        Returns:
            Dict with moa_id, assignments, responses, synthesis result.
        """
        moa_id = _generate_moa_id(task)
        n_workers = max(1, min(n_workers, len(WORKER_NAMES), len(PERSONAS)))

        # Select personas (first N from the list)
        selected_personas = PERSONAS[:n_workers]

        # Find idle workers
        idle = _get_idle_workers()
        if len(idle) < n_workers:
            # Use whatever we have
            available = idle if idle else WORKER_NAMES[:n_workers]
        else:
            available = idle[:n_workers]

        # Assign personas to workers
        assignments = []
        for i, worker in enumerate(available[:n_workers]):
            persona = selected_personas[i % len(selected_personas)]
            assignments.append({
                "worker": worker,
                "persona": persona["id"],
                "persona_label": persona["label"],
                "preamble": persona["preamble"],
            })

        # Build session record
        session = {
            "moa_id": moa_id,
            "task": task,
            "n_workers": len(assignments),
            "assignments": assignments,
            "state": "planned" if dry_run else "dispatching",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeout": timeout,
        }

        if dry_run:
            session["state"] = "dry_run"
            return session

        # ── Phase 1: Dispatch to all workers with persona preambles ──
        from tools.skynet_dispatch import (
            dispatch_to_worker, load_workers, load_orch_hwnd,
        )
        workers = load_workers()
        orch_hwnd = load_orch_hwnd()

        dispatched = []
        for assignment in assignments:
            persona_task = (
                f"[MoA Session {moa_id}]\n"
                f"{assignment['preamble']}\n\n"
                f"TASK: {task}\n\n"
                f"When done, include '{moa_id}' in your bus result content "
                f"so the MoA synthesizer can collect your response."
            )
            try:
                result = dispatch_to_worker(
                    assignment["worker"], persona_task, workers, orch_hwnd
                )
                dispatched.append(assignment["worker"])
                time.sleep(1.5)  # clipboard cooldown between dispatches
            except Exception as e:
                session.setdefault("errors", []).append(
                    f"{assignment['worker']}: {e}"
                )

        session["dispatched"] = dispatched
        session["state"] = "collecting"

        # Save state
        self._state["sessions"][moa_id] = session
        _save_moa_state(self._state)

        # ── Phase 2: Collect responses ──
        responses = self.collect_responses(moa_id, dispatched, timeout)
        session["responses"] = {
            w: r[:500] + "..." if len(r) > 500 else r
            for w, r in responses.items()
        }
        session["responses_received"] = len(responses)

        if not responses:
            session["state"] = "failed_no_responses"
            self._state["sessions"][moa_id] = session
            _save_moa_state(self._state)
            return session

        # ── Phase 3: Synthesize ──
        synthesis_result = self.synthesize(
            moa_id, task, assignments, responses, workers, orch_hwnd
        )
        session["synthesis"] = synthesis_result
        session["state"] = "completed"
        session["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

        # Archive to history
        self._state["history"].append({
            "moa_id": moa_id,
            "task": task[:120],
            "n_workers": len(dispatched),
            "responses_received": len(responses),
            "synthesizer": synthesis_result.get("aggregator"),
            "completed_at": session["completed_at"],
        })
        if len(self._state["history"]) > 100:
            self._state["history"] = self._state["history"][-100:]

        self._state["sessions"][moa_id] = session
        _save_moa_state(self._state)

        return session
        # signed: alpha

    def collect_responses(
        self,
        moa_id: str,
        expected_workers: List[str],
        timeout: float = 120.0,
    ) -> Dict[str, str]:
        """Collect worker responses for a MoA session from the bus.

        Args:
            moa_id: Session identifier to match in response content.
            expected_workers: Worker names we dispatched to.
            timeout: Max seconds to wait for all responses.

        Returns:
            Dict mapping worker_name -> response content.
        """
        return _poll_bus_for_results(moa_id, expected_workers, timeout)
        # signed: alpha

    def synthesize(
        self,
        moa_id: str,
        original_task: str,
        assignments: List[Dict],
        responses: Dict[str, str],
        workers: Optional[list] = None,
        orch_hwnd: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create and dispatch synthesis task combining all perspectives.

        Picks an aggregator worker (preferring one not used in Phase 1)
        and sends it a synthesis prompt with all collected responses.

        Args:
            moa_id: Session ID.
            original_task: The original task description.
            assignments: Persona assignments from Phase 1.
            responses: Collected responses from Phase 2.
            workers: Worker list (loaded if None).
            orch_hwnd: Orchestrator HWND (loaded if None).

        Returns:
            Dict with aggregator worker name and dispatch status.
        """
        from tools.skynet_dispatch import (
            dispatch_to_worker, load_workers, load_orch_hwnd,
        )
        if workers is None:
            workers = load_workers()
        if orch_hwnd is None:
            orch_hwnd = load_orch_hwnd()

        # Pick aggregator: prefer idle worker NOT used in Phase 1
        phase1_workers = {a["worker"] for a in assignments}
        idle = _get_idle_workers()
        aggregator_candidates = [w for w in idle if w not in phase1_workers]
        if not aggregator_candidates:
            # Fall back to any idle worker, or first phase1 worker
            aggregator_candidates = idle if idle else list(phase1_workers)
        aggregator = aggregator_candidates[0] if aggregator_candidates else "alpha"

        # Build synthesis prompt with all responses
        response_sections = []
        for assignment in assignments:
            w = assignment["worker"]
            if w in responses:
                response_sections.append(
                    f"── {assignment['persona_label']} ({w}) ──\n"
                    f"{responses[w]}\n"
                )

        synthesis_task = (
            f"[MoA Synthesis — Session {moa_id}]\n"
            f"{SYNTHESIS_PREAMBLE}\n\n"
            f"ORIGINAL TASK: {original_task}\n\n"
            f"{'=' * 60}\n"
            f"PERSPECTIVES RECEIVED:\n\n"
            + "\n".join(response_sections)
            + f"\n{'=' * 60}\n\n"
            f"Produce the final synthesized result. Include '{moa_id}' "
            f"in your bus result content."
        )

        try:
            dispatch_to_worker(aggregator, synthesis_task, workers, orch_hwnd)
            return {
                "aggregator": aggregator,
                "dispatched": True,
                "prompt_length": len(synthesis_task),
            }
        except Exception as e:
            return {
                "aggregator": aggregator,
                "dispatched": False,
                "error": str(e),
            }
        # signed: alpha

    def get_status(self, moa_id: Optional[str] = None) -> Dict[str, Any]:
        """Get status of MoA session(s)."""
        state = _load_moa_state()
        if moa_id:
            session = state["sessions"].get(moa_id)
            return session if session else {"error": f"Unknown moa_id '{moa_id}'"}
        active = {
            k: v for k, v in state["sessions"].items()
            if v.get("state") not in ("completed", "failed_no_responses", "dry_run")
        }
        return {"active_sessions": len(active), "sessions": active}

    def get_history(self, limit: int = 20) -> List[Dict]:
        """Get recent MoA session history."""
        state = _load_moa_state()
        return state.get("history", [])[-limit:]


# ── CLI ───────────────────────────────────────────────────────────

def _cli():
    """CLI entry point for MoA dispatch."""
    parser = argparse.ArgumentParser(
        description="Skynet Mixture of Agents (MoA) dispatch",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
MoA sends the same task to N workers with different personas (analyzer,
implementer, critic, architect), collects all responses, then dispatches
a synthesis task to an aggregator worker.

Examples:
    python tools/skynet_moa.py "Fix auth middleware" --n 3
    python tools/skynet_moa.py "Optimize hot loop" --n 4 --timeout 180
    python tools/skynet_moa.py "Design cache" --dry-run
    python tools/skynet_moa.py --status
    python tools/skynet_moa.py --history
""",
    )
    parser.add_argument("task", nargs="?", help="Task to dispatch via MoA")
    parser.add_argument(
        "--n", type=int, default=3,
        help="Number of workers/personas (1-4, default 3)",
    )
    parser.add_argument(
        "--timeout", type=float, default=120.0,
        help="Timeout for collecting responses (default 120s)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show dispatch plan without executing",
    )
    parser.add_argument(
        "--status", type=str, nargs="?", const="__all__",
        help="Show status of MoA session(s)",
    )
    parser.add_argument(
        "--history", action="store_true",
        help="Show recent MoA history",
    )

    args = parser.parse_args()
    moa = MoADispatch()

    if args.history:
        history = moa.get_history()
        if not history:
            print("No MoA history yet.")
        else:
            for h in history:
                print(
                    f"  {h.get('completed_at', '?')} | {h.get('n_workers', 0)} workers "
                    f"| {h.get('responses_received', 0)} responses "
                    f"| synth={h.get('synthesizer', '?')} "
                    f"| {h.get('task', '?')[:60]}"
                )
        return

    if args.status is not None:
        sid = None if args.status == "__all__" else args.status
        result = moa.get_status(sid)
        print(json.dumps(result, indent=2, default=str))
        return

    if not args.task:
        parser.print_help()
        return

    print(f"MoA dispatch: {args.n} workers, timeout={args.timeout}s")
    if args.dry_run:
        print("DRY RUN — no actual dispatch")

    result = moa.dispatch_moa(
        args.task, n_workers=args.n, timeout=args.timeout, dry_run=args.dry_run
    )

    print(f"\nMoA ID: {result['moa_id']}")
    print(f"State:  {result['state']}")
    print(f"Workers: {result.get('n_workers', 0)}")
    print("\nAssignments:")
    for a in result.get("assignments", []):
        print(f"  {a['worker']:8s} -> {a['persona_label']}")

    if not args.dry_run:
        dispatched = result.get("dispatched", [])
        responses = result.get("responses_received", 0)
        print(f"\nDispatched: {len(dispatched)}")
        print(f"Responses:  {responses}")
        synth = result.get("synthesis", {})
        if synth.get("dispatched"):
            print(f"Synthesizer: {synth['aggregator']}")
        errors = result.get("errors", [])
        if errors:
            print(f"Errors: {errors}")


if __name__ == "__main__":
    _cli()
# signed: alpha
