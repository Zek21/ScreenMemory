#!/usr/bin/env python3
"""
Send a harmless consultant test prompt first and block the real invoke unless
 direct ghost-type delivery actually succeeded.
"""  # signed: consultant

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.skynet_delivery import (
    _consultant_hwnd_is_valid,
    _load_consultant_state,
    deliver_to_consultant,
)


def _route_snapshot(consultant_id: str) -> Dict[str, Any]:
    state = _load_consultant_state(consultant_id)
    return {
        "consultant_id": consultant_id,
        "hwnd": int(state.get("hwnd") or 0),
        "prompt_transport": state.get("prompt_transport"),
        "transport": state.get("transport"),
        "live": bool(state.get("live")),
        "accepts_prompts": bool(state.get("accepts_prompts")),
        "routable": bool(state.get("routable")),
        "hwnd_valid": _consultant_hwnd_is_valid(state, consultant_id),
        "api_url": state.get("api_url"),
    }


def _extract_prompt_id(test_result: Dict[str, Any]) -> str:
    detail = str(test_result.get("detail") or "")
    marker = "prompt_id="
    if marker not in detail:
        return ""
    tail = detail.split(marker, 1)[1]
    return tail.split(",", 1)[0].strip()


def _recent_bus_messages(limit: int = 40) -> list[dict]:
    try:
        with urllib.request.urlopen(
            f"http://localhost:8420/bus/messages?limit={limit}", timeout=5
        ) as resp:
            data = json.loads(resp.read())
        if isinstance(data, list):
            return [m for m in data if isinstance(m, dict)]
        if isinstance(data, dict):
            for key in ("messages", "value"):
                val = data.get(key)
                if isinstance(val, list):
                    return [m for m in val if isinstance(m, dict)]
    except Exception:
        pass
    return []


def _verify_bridge_queue_delivery(consultant_id: str, prompt_id: str,
                                  timeout_s: float = 20.0,
                                  poll_s: float = 1.0) -> Dict[str, Any]:
    """Verify queued consultant delivery using real evidence.

    Truthful success criteria for bridge_queue mode:
    - a task_claim/result bus message references the prompt_id, or
    - consultant state file task_state matches the prompt_id and moved to a live status.
    """
    deadline = time.time() + timeout_s
    state_file = ROOT / "data" / (
        "consultant_state.json" if consultant_id == "consultant"
        else f"{consultant_id}_state.json"
    )
    seen_reasons = []

    while time.time() < deadline:
        for msg in _recent_bus_messages():
            if msg.get("sender") != consultant_id:
                continue
            content = str(msg.get("content") or "")
            if prompt_id and prompt_id in content:
                return {
                    "success": True,
                    "verification": "bus",
                    "type": msg.get("type", ""),
                    "content": content[:220],
                }

        try:
            if state_file.exists():
                state = json.loads(state_file.read_text(encoding="utf-8"))
                task_state = state.get("task_state") or {}
                if isinstance(task_state, dict) and task_state.get("prompt_id") == prompt_id:
                    status = str(task_state.get("status") or "").upper()
                    if status in {"CLAIMED", "WORKING", "IN_PROGRESS", "COMPLETED"}:
                        return {
                            "success": True,
                            "verification": "state_file",
                            "status": status,
                            "notes": str(task_state.get("notes") or "")[:220],
                        }
                    seen_reasons.append(status or "task_state_seen")
        except Exception:
            pass

        time.sleep(poll_s)

    return {
        "success": False,
        "verification": "timeout",
        "reason": ",".join(seen_reasons[-3:]) or "no_claim_or_result_seen",
    }  # signed: consultant


def run_gate(consultant_id: str, test_prompt: str, real_prompt: str = "",
             sender: str = "consultant_gate") -> Dict[str, Any]:
    snapshot = _route_snapshot(consultant_id)
    test_result = deliver_to_consultant(
        consultant_id,
        test_prompt,
        sender=sender,
        msg_type="test_prompt",
    )
    prompt_id = _extract_prompt_id(test_result)
    verified_delivery = None
    gate_success = bool(test_result.get("success"))

    if not gate_success and test_result.get("delivery_status") == "queued":
        verified_delivery = _verify_bridge_queue_delivery(consultant_id, prompt_id)
        gate_success = bool(verified_delivery.get("success"))

    result: Dict[str, Any] = {
        "snapshot": snapshot,
        "test_prompt": test_prompt,
        "test_result": test_result,
        "prompt_id": prompt_id,
        "verified_delivery": verified_delivery,
        "gate_success": gate_success,
        "real_prompt_sent": False,
        "real_prompt_result": None,
        "blocked": not gate_success,
    }
    if real_prompt:
        if gate_success:
            real_result = deliver_to_consultant(
                consultant_id,
                real_prompt,
                sender=sender,
                msg_type="directive",
            )
            real_prompt_id = _extract_prompt_id(real_result)
            result["real_prompt_result"] = real_result
            result["real_prompt_prompt_id"] = real_prompt_id
            if not real_result.get("success") and real_result.get("delivery_status") == "queued":
                result["real_prompt_verified_delivery"] = _verify_bridge_queue_delivery(
                    consultant_id, real_prompt_id
                )
            result["real_prompt_sent"] = True
        else:
            result["block_reason"] = (
                "Test prompt did not achieve a verified consultant route. "
                "Real invoke aborted."
            )
    return result  # signed: consultant


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Consultant prompt gate: test direct delivery before real invoke."
    )
    parser.add_argument("--consultant-id", required=True, help="consultant or gemini_consultant")
    parser.add_argument("--test-prompt", required=True, help="Harmless test prompt to send first")
    parser.add_argument("--real-prompt", default="", help="Real prompt to send only after test delivery succeeds")
    parser.add_argument("--sender", default="consultant_gate", help="Bus sender identity for audit trail")
    args = parser.parse_args()

    result = run_gate(
        consultant_id=args.consultant_id,
        test_prompt=args.test_prompt,
        real_prompt=args.real_prompt,
        sender=args.sender,
    )
    print(json.dumps(result, indent=2))
    if args.real_prompt and not result.get("real_prompt_sent"):
        return 2
    if not result.get("gate_success"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
