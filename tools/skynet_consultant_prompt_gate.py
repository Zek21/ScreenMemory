#!/usr/bin/env python3
"""
Send a harmless consultant test prompt first and block the real invoke unless
 direct ghost-type delivery actually succeeded.
"""  # signed: consultant

from __future__ import annotations

import argparse
import json
import sys
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


def run_gate(consultant_id: str, test_prompt: str, real_prompt: str = "",
             sender: str = "consultant_gate") -> Dict[str, Any]:
    snapshot = _route_snapshot(consultant_id)
    test_result = deliver_to_consultant(
        consultant_id,
        test_prompt,
        sender=sender,
        msg_type="test_prompt",
    )
    result: Dict[str, Any] = {
        "snapshot": snapshot,
        "test_prompt": test_prompt,
        "test_result": test_result,
        "real_prompt_sent": False,
        "real_prompt_result": None,
        "blocked": not bool(test_result.get("success")),
    }
    if real_prompt:
        if test_result.get("success"):
            result["real_prompt_result"] = deliver_to_consultant(
                consultant_id,
                real_prompt,
                sender=sender,
                msg_type="directive",
            )
            result["real_prompt_sent"] = True
        else:
            result["block_reason"] = (
                "Test prompt did not achieve direct delivery. "
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
    if not result.get("test_result", {}).get("success"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
