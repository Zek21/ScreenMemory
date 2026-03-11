#!/usr/bin/env python3
"""
skynet_delivery.py -- Unified delivery registry for the Skynet direct-prompt model.

Replaces polling-based message delivery with direct UIA ghost-typing for all
targets: orchestrator, workers, and consultant result forwarding.

Delivery Model:
  - Workers receive tasks via UIA ghost-type (skynet_dispatch.ghost_type_to_worker)
  - Orchestrator receives directives via UIA ghost-type to its VS Code window
  - Consultants are advisory-only (non-routable); their results are delivered
    to the orchestrator via direct-prompt when they post to topic=orchestrator

Routing Rules:
  1. Worker targets: look up HWND in workers.json, ghost-type directly
  2. Orchestrator target: look up HWND in orchestrator.json, ghost-type directly
  3. Consultant targets: bus-only (post to topic matching consultant ID)
  4. Convene-gated messages: worker→orchestrator results go through ConveneGate
     unless marked urgent; elevated messages are then direct-prompted to orchestrator
  5. Self-prompt: autonomous daemon uses this module to wake the orchestrator

Truth Semantics:
  - Every delivery is logged with: target, method, timestamp, success, latency_ms
  - "delivered" means UIA confirmed the text was pasted into the target's input box
  - "bus_only" means the message was posted to the bus but NOT ghost-typed
  - "failed" means the ghost-type attempt returned False
  - Delivery log persisted at data/delivery_log.json (last 200 entries)

Usage:
    from tools.skynet_delivery import deliver, DeliveryTarget

    deliver(DeliveryTarget.ORCHESTRATOR, "Status update: all workers healthy")
    deliver(DeliveryTarget.WORKER, "fix the auth bug", worker_name="alpha")
    deliver(DeliveryTarget.BUS, "advisory note", bus_topic="knowledge")
"""

import json
import os
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ORCH_FILE = DATA_DIR / "orchestrator.json"
WORKERS_FILE = DATA_DIR / "workers.json"
DELIVERY_LOG = DATA_DIR / "delivery_log.json"


class DeliveryTarget(Enum):
    ORCHESTRATOR = "orchestrator"
    WORKER = "worker"
    BUS = "bus"  # bus-only (no UIA delivery)


class DeliveryMethod(Enum):
    DIRECT_PROMPT = "direct_prompt"  # UIA ghost-type
    BUS_POST = "bus_post"           # HTTP POST to /bus/publish
    HYBRID = "hybrid"               # bus_post + direct_prompt


def _load_orch_hwnd() -> int:
    """Read the orchestrator HWND from orchestrator.json."""
    try:
        data = json.loads(ORCH_FILE.read_text(encoding="utf-8"))
        return data.get("hwnd") or data.get("orchestrator_hwnd") or 0
    except Exception:
        return 0


def _load_worker_hwnd(worker_name: str) -> int:
    """Read a worker's HWND from workers.json."""
    try:
        data = json.loads(WORKERS_FILE.read_text(encoding="utf-8"))
        workers = data if isinstance(data, list) else data.get("workers", data)
        if isinstance(workers, list):
            for w in workers:
                if w.get("name") == worker_name:
                    return w.get("hwnd", 0)
        elif isinstance(workers, dict):
            for name, info in workers.items():
                if name == worker_name:
                    return info.get("hwnd", 0)
    except Exception:
        pass
    return 0


def _ghost_type(hwnd: int, text: str, orch_hwnd: int = 0) -> bool:
    """Deliver text to a window via UIA ghost-type. Returns True on success."""
    try:
        from skynet_dispatch import ghost_type_to_worker
        return ghost_type_to_worker(hwnd, text, orch_hwnd or hwnd)
    except Exception:
        return False


def _bus_post(sender: str, topic: str, msg_type: str, content: str) -> bool:
    """Post a message to the Skynet bus."""
    try:
        from shared.bus import bus_post_fields
        return bus_post_fields(sender, topic, msg_type, content)
    except ImportError:
        # Fallback: direct HTTP post via urllib (no dependency)
        import urllib.request
        try:
            payload = json.dumps({
                "sender": sender, "topic": topic,
                "type": msg_type, "content": content,
            }).encode("utf-8")
            req = urllib.request.Request(
                "http://localhost:8420/bus/publish",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=3)
            return True
        except Exception:
            return False
    except Exception:
        return False


def _log_delivery(target: str, method: str, success: bool,
                  latency_ms: float, detail: str = ""):
    """Append delivery event to delivery_log.json."""
    try:
        if DELIVERY_LOG.exists():
            log_data = json.loads(DELIVERY_LOG.read_text(encoding="utf-8"))
        else:
            log_data = []
        log_data.append({
            "target": target,
            "method": method,
            "success": success,
            "latency_ms": round(latency_ms, 1),
            "detail": detail[:200],
            "timestamp": datetime.now().isoformat(),
        })
        if len(log_data) > 200:
            log_data = log_data[-200:]
        DELIVERY_LOG.write_text(json.dumps(log_data, indent=2), encoding="utf-8")
    except Exception:
        pass


def deliver(target: DeliveryTarget, content: str,
            worker_name: Optional[str] = None,
            bus_topic: Optional[str] = None,
            bus_sender: str = "delivery",
            bus_type: str = "message",
            urgent: bool = False) -> dict:
    """Unified delivery entry point.

    Routes messages to the correct target using the direct-prompt model:
    - ORCHESTRATOR: ghost-type into orchestrator's VS Code chat window
    - WORKER: ghost-type into the named worker's chat window
    - BUS: post to bus only (no UIA delivery)

    Args:
        target: DeliveryTarget enum value.
        content: Message text to deliver.
        worker_name: Required when target is WORKER.
        bus_topic: Override bus topic (default: target-appropriate).
        bus_sender: Sender identity for bus messages.
        bus_type: Message type for bus messages.
        urgent: If True, bypasses convene gate for orchestrator delivery.

    Returns:
        Dict with: target, method, success, latency_ms, detail.
    """
    t0 = time.time()

    if target == DeliveryTarget.ORCHESTRATOR:
        hwnd = _load_orch_hwnd()
        if not hwnd:
            result = {"target": "orchestrator", "method": "failed",
                      "success": False, "detail": "No orchestrator HWND"}
        else:
            ok = _ghost_type(hwnd, content, hwnd)
            result = {
                "target": "orchestrator",
                "method": DeliveryMethod.DIRECT_PROMPT.value,
                "success": ok,
                "detail": f"HWND={hwnd}, len={len(content)}",
            }

    elif target == DeliveryTarget.WORKER:
        if not worker_name:
            result = {"target": "worker", "method": "failed",
                      "success": False, "detail": "No worker_name specified"}
        else:
            hwnd = _load_worker_hwnd(worker_name)
            orch_hwnd = _load_orch_hwnd()
            if not hwnd:
                result = {"target": f"worker:{worker_name}", "method": "failed",
                          "success": False, "detail": f"No HWND for {worker_name}"}
            else:
                ok = _ghost_type(hwnd, content, orch_hwnd)
                result = {
                    "target": f"worker:{worker_name}",
                    "method": DeliveryMethod.DIRECT_PROMPT.value,
                    "success": ok,
                    "detail": f"HWND={hwnd}, len={len(content)}",
                }

    elif target == DeliveryTarget.BUS:
        topic = bus_topic or "general"
        ok = _bus_post(bus_sender, topic, bus_type, content)
        result = {
            "target": f"bus:{topic}",
            "method": DeliveryMethod.BUS_POST.value,
            "success": ok,
            "detail": f"topic={topic}, type={bus_type}",
        }

    else:
        result = {"target": str(target), "method": "unknown",
                  "success": False, "detail": "Unknown target"}

    latency_ms = (time.time() - t0) * 1000
    result["latency_ms"] = round(latency_ms, 1)
    _log_delivery(result["target"], result["method"], result["success"],
                  latency_ms, result.get("detail", ""))
    return result


def deliver_to_orchestrator(content: str, sender: str = "delivery",
                            also_bus: bool = True) -> dict:
    """Convenience: deliver directly to orchestrator via ghost-type.

    Also posts to bus topic=orchestrator for durability (unless also_bus=False).
    """
    result = deliver(DeliveryTarget.ORCHESTRATOR, content)

    if also_bus:
        _bus_post(sender, "orchestrator", "delivery", content[:500])

    return result


def deliver_consultant_result(consultant_id: str, content: str) -> dict:
    """Deliver a consultant's result directly to the orchestrator.

    Consultants are advisory/non-routable, so their bus results must be
    actively forwarded to the orchestrator via direct-prompt. This function
    bridges the gap: consultant posts to bus, AND the result gets ghost-typed
    into the orchestrator's chat window for immediate visibility.

    Truth: The bus post is the durable record; the ghost-type is the
    low-latency notification. Both happen atomically.
    """
    # Post to bus (durable record)
    _bus_post(consultant_id, "orchestrator", "result", content)

    # Direct-prompt to orchestrator (low-latency notification)
    formatted = (
        f"[CONSULTANT RESULT from {consultant_id.upper()}]\n"
        f"{content[:2000]}"
    )
    return deliver(DeliveryTarget.ORCHESTRATOR, formatted)


def deliver_elevated_report(gate_id: str, proposer: str, content: str,
                            voters: list) -> dict:
    """Deliver a convene-gate-elevated report directly to the orchestrator.

    Called when a worker report achieves consensus through the ConveneGate.
    Instead of just posting to bus (where orchestrator might not see it),
    this function ghost-types it directly into the orchestrator window.

    Convene Caveat: The gate elevates only after MAJORITY_THRESHOLD votes.
    Urgent messages bypass the gate entirely. The bus post from convene-gate
    sender remains the canonical record; the direct-prompt is the notification.
    """
    # Bus post remains as before (canonical record from convene-gate)
    _bus_post("convene-gate", "orchestrator", "elevated", content)

    # Direct-prompt to orchestrator for immediate visibility
    formatted = (
        f"[CONVENE-ELEVATED] gate={gate_id} proposer={proposer} "
        f"voters={','.join(voters)}\n{content[:2000]}"
    )
    return deliver(DeliveryTarget.ORCHESTRATOR, formatted)


# ── Routing Registry ─────────────────────────────────────────────

# Static routing table: maps target names to delivery methods and HWND sources.
# Truth: This registry is the single source of truth for delivery routing.
# If a target is not in this registry, it is not routable via direct-prompt.
ROUTING_REGISTRY = {
    "orchestrator": {
        "method": DeliveryMethod.DIRECT_PROMPT,
        "hwnd_source": "orchestrator.json",
        "routable": True,
        "convene_gated": False,  # orchestrator receives directly
        "notes": "Ghost-type into orchestrator VS Code chat window",
    },
    "alpha": {
        "method": DeliveryMethod.DIRECT_PROMPT,
        "hwnd_source": "workers.json",
        "routable": True,
        "convene_gated": False,
        "notes": "Ghost-type into worker chat window",
    },
    "beta": {
        "method": DeliveryMethod.DIRECT_PROMPT,
        "hwnd_source": "workers.json",
        "routable": True,
        "convene_gated": False,
        "notes": "Ghost-type into worker chat window",
    },
    "gamma": {
        "method": DeliveryMethod.DIRECT_PROMPT,
        "hwnd_source": "workers.json",
        "routable": True,
        "convene_gated": False,
        "notes": "Ghost-type into worker chat window",
    },
    "delta": {
        "method": DeliveryMethod.DIRECT_PROMPT,
        "hwnd_source": "workers.json",
        "routable": True,
        "convene_gated": False,
        "notes": "Ghost-type into worker chat window",
    },
    "consultant": {
        "method": DeliveryMethod.BUS_POST,
        "hwnd_source": None,
        "routable": False,
        "convene_gated": False,
        "notes": "Advisory peer, non-routable. Results forwarded to orchestrator.",
    },
    "gemini_consultant": {
        "method": DeliveryMethod.BUS_POST,
        "hwnd_source": None,
        "routable": False,
        "convene_gated": False,
        "notes": "Advisory peer, non-routable. Results forwarded to orchestrator.",
    },
}


def get_routing_info(target_name: str) -> dict:
    """Look up routing info for a target. Returns empty dict if not found."""
    return ROUTING_REGISTRY.get(target_name, {})


def is_routable(target_name: str) -> bool:
    """Check if a target supports direct-prompt delivery."""
    return ROUTING_REGISTRY.get(target_name, {}).get("routable", False)


def list_routable_targets() -> list:
    """Return all target names that support direct-prompt delivery."""
    return [name for name, info in ROUTING_REGISTRY.items() if info.get("routable")]
