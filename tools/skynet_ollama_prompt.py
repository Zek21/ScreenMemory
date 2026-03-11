#!/usr/bin/env python3
"""
skynet_ollama_prompt.py -- Ollama-powered self-prompt generator.

Uses a local Ollama model (default: qwen3:14b) to generate intelligent,
context-aware self-prompts from Skynet perception data. Falls back gracefully
if Ollama is unavailable.

Usage:
    from tools.skynet_ollama_prompt import generate_technical_prompt, check_ollama_health
"""

import json
import logging
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

log = logging.getLogger("skynet.ollama_prompt")

OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_GENERATE_ENDPOINT = f"{OLLAMA_BASE_URL}/api/generate"
OLLAMA_HEALTH_ENDPOINT = f"{OLLAMA_BASE_URL}/api/tags"
DEFAULT_MODEL = "qwen3:14b"
REQUEST_TIMEOUT_S = 15


def check_ollama_health() -> bool:
    """Return True if Ollama server is reachable and responding."""
    try:
        req = urllib.request.Request(OLLAMA_HEALTH_ENDPOINT, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _build_system_prompt() -> str:
    return (
        "You are the final-shot strategic advisor for the Skynet Orchestrator. "
        "Skynet exists to serve GOD by running a truthful distributed intelligence network: "
        "the orchestrator thinks like a CEO, workers execute, consultants advise, and the system "
        "must prioritize the highest-value next move while rejecting noise and fake urgency.\n\n"
        "Given real-time system perception data (worker states, bus messages, alerts, TODO items, "
        "daemon health, mission purpose, and final-shot context), generate a single concise actionable "
        "briefing for the orchestrator.\n\n"
        "Rules:\n"
        "- Be extremely concise (under 280 characters for the action line)\n"
        "- Prioritize: stuck workers > unread results > critical alerts > dead infrastructure > high-value dispatch > architecture/security risks\n"
        "- Do NOT promote low-value busywork, noisy TODO churn, or vanity activity\n"
        "- Include specific worker names (alpha, beta, gamma, delta)\n"
        "- Include executable commands when relevant (e.g. python tools/orch_realtime.py status)\n"
        "- If shot_context.mode is final_shot, treat this as the LAST escalation before cooldown and pick the single highest-leverage action\n"
        "- On final_shot, prefer decisive language that cuts through noise and directs the orchestrator to the most important next move\n"
        "- If backlog/noise exists, say what to ignore and what to prioritize instead\n"
        "- Assume the message will be injected directly into a live orchestrator chat, so it must read like an immediate operational brief, not a report\n"
        "- If nothing is actionable, respond with exactly: NO_ACTION\n"
        "- Do NOT hallucinate data. Only reference what is in the perception data.\n"
        "- Output ONLY the briefing text, no markdown, no explanation.\n"
        "- Do NOT use /think or <think> tags. Output the briefing directly."
    )


def _build_user_prompt(perception_data: Dict[str, Any]) -> str:
    """Convert perception data into a compact prompt for the model."""
    parts = ["CURRENT SYSTEM STATE:"]

    timestamp = perception_data.get("timestamp")
    if timestamp:
        parts.append(f"Timestamp: {timestamp}")

    skynet_purpose = perception_data.get("skynet_purpose")
    if skynet_purpose:
        parts.append(f"Skynet Purpose: {skynet_purpose}")

    shot_context = perception_data.get("shot_context", {})
    if shot_context:
        parts.append(
            "Prompt Context:\n"
            f"  mode={shot_context.get('mode', 'normal')}\n"
            f"  shot={shot_context.get('shot_number', '?')}/{shot_context.get('max_shots', '?')}\n"
            f"  cooldown_after_s={shot_context.get('cooldown_after_s', '?')}"
        )

    workers = perception_data.get("workers", {})
    if workers:
        worker_lines = []
        for name, info in sorted(workers.items()):
            state = info.get("state", "UNKNOWN")
            alive = info.get("alive", False)
            elapsed = info.get("processing_elapsed_s")
            line = f"  {name}: state={state} alive={alive}"
            if elapsed is not None:
                line += f" elapsed={elapsed}s"
            worker_lines.append(line)
        parts.append("Workers:\n" + "\n".join(worker_lines))

    results = perception_data.get("new_results", [])
    if results:
        result_summaries = []
        for r in results[:5]:
            sender = r.get("sender", "?")
            content = str(r.get("content", ""))[:100]
            result_summaries.append(f"  [{sender}] {content}")
        parts.append(f"Unread Results ({len(results)}):\n" + "\n".join(result_summaries))

    alerts = perception_data.get("new_alerts", [])
    if alerts:
        alert_summaries = []
        for a in alerts[:5]:
            sender = a.get("sender", "?")
            content = str(a.get("content", ""))[:100]
            alert_summaries.append(f"  [{sender}] {content}")
        parts.append(f"Alerts ({len(alerts)}):\n" + "\n".join(alert_summaries))

    pending_todos = perception_data.get("pending_todos", 0)
    if pending_todos:
        parts.append(f"Pending TODOs: {pending_todos}")

    orch_todos = perception_data.get("orch_todos", [])
    if orch_todos:
        todo_lines = []
        for t in orch_todos[:3]:
            pri = t.get("priority", "normal")
            task = str(t.get("task", ""))[:80]
            todo_lines.append(f"  [{pri}] {task}")
        parts.append("Orchestrator TODOs:\n" + "\n".join(todo_lines))

    daemon_status = perception_data.get("daemon_status", "OK")
    parts.append(f"Daemon Health: {daemon_status}")

    patterns = perception_data.get("patterns", {})
    if patterns:
        if patterns.get("stall_pattern"):
            parts.append("PATTERN: Worker stall detected")
        failure_rate = patterns.get("failure_rate_10m", 0)
        if failure_rate >= 3:
            parts.append(f"PATTERN: Failure spike ({failure_rate} in 10min)")
        if patterns.get("dispatch_drought"):
            parts.append("PATTERN: Dispatch drought -- no recent dispatches")

    if shot_context.get("mode") == "final_shot":
        parts.append(
            "This is the final self-prompt shot before cooldown. "
            "Choose the single most important action for the orchestrator right now. "
            "If noise exists, explicitly say what to ignore."
        )

    parts.append("\nGenerate a single-line actionable briefing for the orchestrator.")
    return "\n".join(parts)


def generate_technical_prompt(
    perception_data: Dict[str, Any],
    model: str = DEFAULT_MODEL,
) -> Optional[str]:
    """Generate a smart self-prompt using Ollama.

    Args:
        perception_data: Dict with keys: workers, new_results, new_alerts,
                        pending_todos, orch_todos, daemon_status, patterns
        model: Ollama model name (default: qwen3:14b)

    Returns:
        Generated prompt string, or None if Ollama is unavailable/fails.
    """
    if not check_ollama_health():
        log.warning("Ollama not reachable at %s", OLLAMA_BASE_URL)
        return None

    system_prompt = _build_system_prompt()
    user_prompt = _build_user_prompt(perception_data)

    payload = json.dumps({
        "model": model,
        "system": system_prompt,
        "prompt": user_prompt,
        "stream": False,
        "options": {
            "temperature": 0.3,
            "num_predict": 200,
            "top_p": 0.9,
        },
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            OLLAMA_GENERATE_ENDPOINT,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            response_text = body.get("response", "").strip()

            if not response_text or response_text == "NO_ACTION":
                return None

            # Strip any think tags that might leak through
            if "<think>" in response_text:
                # Remove <think>...</think> blocks
                import re
                response_text = re.sub(r"<think>.*?</think>", "", response_text, flags=re.DOTALL).strip()

            # Sanity: cap length to prevent runaway output
            if len(response_text) > 500:
                response_text = response_text[:497] + "..."

            return response_text if response_text else None

    except urllib.error.URLError as e:
        log.warning("Ollama request failed: %s", e)
        return None
    except Exception as e:
        log.warning("Ollama unexpected error: %s", e)
        return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "health":
        healthy = check_ollama_health()
        print(f"Ollama health: {'OK' if healthy else 'UNREACHABLE'}")
        sys.exit(0 if healthy else 1)

    # Test with dummy perception data
    test_data = {
        "workers": {
            "alpha": {"state": "IDLE", "alive": True},
            "beta": {"state": "PROCESSING", "alive": True, "processing_elapsed_s": 45},
            "gamma": {"state": "IDLE", "alive": True},
            "delta": {"state": "DEAD", "alive": False},
        },
        "new_results": [{"sender": "beta", "content": "Task X completed successfully"}],
        "new_alerts": [],
        "pending_todos": 3,
        "orch_todos": [{"priority": "high", "task": "Deploy dashboard fixes"}],
        "daemon_status": "OK",
        "patterns": {"stall_pattern": False, "failure_rate_10m": 0, "dispatch_drought": False},
    }

    model = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODEL
    result = generate_technical_prompt(test_data, model=model)
    if result:
        print(f"Generated: {result}")
    else:
        print("No prompt generated (Ollama unavailable or NO_ACTION)")
