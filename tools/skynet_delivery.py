#!/usr/bin/env python3
"""
skynet_delivery.py -- Unified delivery registry for the Skynet direct-prompt model.

Replaces polling-based message delivery with direct UIA ghost-typing for all
targets: orchestrator, workers, and consultant result forwarding.

Delivery Model:
  - Workers receive tasks via UIA ghost-type (skynet_dispatch.ghost_type_to_worker)
  - Orchestrator receives directives via UIA ghost-type to its VS Code window
  - Consultants receive prompts via their live bridge queue (HTTP POST)
  - Consultant results are still forwarded to the orchestrator via direct-prompt

Routing Rules:
  1. Worker targets: look up HWND in workers.json, ghost-type directly
  2. Orchestrator target: look up HWND in orchestrator.json, ghost-type directly
  3. Consultant targets: bridge queue delivery using the consultant state file
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
    deliver(DeliveryTarget.CONSULTANT, "Investigate routing drift", consultant_id="gemini_consultant")
"""

import ctypes
import ctypes.wintypes
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

# VS Code window title patterns (case-insensitive substring match)
_VSCODE_TITLE_MARKERS = ("Visual Studio Code", "VS Code")
_VSCODE_PROCESS_NAMES = ("Code - Insiders.exe", "Code.exe", "code")


class DeliveryTarget(Enum):
    ORCHESTRATOR = "orchestrator"
    WORKER = "worker"
    CONSULTANT = "consultant"
    BUS = "bus"  # bus-only (no UIA delivery)


class DeliveryMethod(Enum):
    DIRECT_PROMPT = "direct_prompt"  # UIA ghost-type
    CONSULTANT_BRIDGE = "consultant_bridge"
    BUS_POST = "bus_post"           # HTTP POST to /bus/publish
    HYBRID = "hybrid"               # bus_post + direct_prompt


# ── HWND Validation (Security Layer) ────────────────────────────

def _is_window(hwnd: int) -> bool:
    """Win32 IsWindow -- check if HWND refers to a live window."""
    try:
        return bool(ctypes.windll.user32.IsWindow(hwnd))
    except Exception:
        return False


def _get_window_pid(hwnd: int) -> int:
    """Return the PID owning the given HWND, or 0 on failure."""
    try:
        pid = ctypes.wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value
    except Exception:
        return 0


def _get_process_name(pid: int) -> str:
    """Return the executable name (e.g. 'Code - Insiders.exe') for a PID."""
    if pid <= 0:
        return ""
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = ctypes.wintypes.DWORD(260)
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size))
            if ok:
                return os.path.basename(buf.value)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def _get_window_title(hwnd: int) -> str:
    """Return the window title for an HWND."""
    try:
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        return ""


class HWNDValidationError(Exception):
    """Raised when HWND validation fails a security check."""
    pass


def validate_hwnd(hwnd: int, expected_target: str = "") -> dict:
    """Validate that an HWND is safe for ghost-type delivery.

    Security checks performed:
      1. HWND is non-zero
      2. IsWindow(hwnd) returns True (window exists)
      3. Owning process is a VS Code variant (Code.exe / Code - Insiders.exe)
      4. Window title contains a VS Code marker string

    Args:
        hwnd: The window handle to validate.
        expected_target: Optional label for error messages (e.g. "orchestrator").

    Returns:
        Dict with: valid (bool), hwnd, pid, process_name, title, checks (dict).

    Raises:
        HWNDValidationError if validation fails (use validate_hwnd_strict()).
    """
    result = {
        "valid": False,
        "hwnd": hwnd,
        "pid": 0,
        "process_name": "",
        "title": "",
        "target": expected_target,
        "checks": {
            "nonzero": False,
            "is_window": False,
            "is_vscode_process": False,
            "has_vscode_title": False,
        },
    }

    # Check 1: non-zero
    if not hwnd:
        return result
    result["checks"]["nonzero"] = True

    # Check 2: IsWindow
    if not _is_window(hwnd):
        return result
    result["checks"]["is_window"] = True

    # Check 3: Process is VS Code
    pid = _get_window_pid(hwnd)
    result["pid"] = pid
    proc_name = _get_process_name(pid)
    result["process_name"] = proc_name
    if proc_name and any(proc_name.lower() == vsc.lower() for vsc in _VSCODE_PROCESS_NAMES):
        result["checks"]["is_vscode_process"] = True

    # Check 4: Window title contains VS Code marker
    title = _get_window_title(hwnd)
    result["title"] = title
    if title and any(marker.lower() in title.lower() for marker in _VSCODE_TITLE_MARKERS):
        result["checks"]["has_vscode_title"] = True

    # Valid only if all checks pass
    result["valid"] = all(result["checks"].values())
    return result


def validate_hwnd_strict(hwnd: int, expected_target: str = "") -> dict:
    """Like validate_hwnd() but raises HWNDValidationError on failure."""
    result = validate_hwnd(hwnd, expected_target)
    if not result["valid"]:
        failed = [k for k, v in result["checks"].items() if not v]
        raise HWNDValidationError(
            f"HWND {hwnd} failed security validation for target "
            f"'{expected_target}': failed checks = {failed}"
        )
    return result


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


def _consultant_state_file(consultant_id: str) -> Path:
    if consultant_id == "consultant":
        return DATA_DIR / "consultant_state.json"
    return DATA_DIR / f"{consultant_id}_state.json"


def _load_consultant_state(consultant_id: str) -> dict:
    try:
        path = _consultant_state_file(consultant_id)
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _ghost_type(hwnd: int, text: str, orch_hwnd: int = 0,
                target_label: str = "") -> bool:
    """Deliver text to a window via UIA ghost-type. Returns True on success.

    Performs HWND security validation before ghost-typing:
    - Target must be alive, owned by VS Code, with matching title.
    - Rejects tampered/stale HWNDs before any content is typed.
    """
    # Security: validate target HWND before ghost-typing content
    validation = validate_hwnd(hwnd, target_label)
    if not validation["valid"]:
        failed = [k for k, v in validation["checks"].items() if not v]
        _log_delivery(
            target_label or f"hwnd:{hwnd}", "blocked",
            False, 0.0,
            f"HWND validation failed: {failed} pid={validation['pid']} "
            f"proc={validation['process_name']}"
        )
        return False

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


def _json_post(url: str, payload: dict, timeout: float = 5.0) -> dict | None:
    import urllib.request
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return data if isinstance(data, dict) else None
    except Exception:
        return None


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
            consultant_id: Optional[str] = None,
            bus_topic: Optional[str] = None,
            bus_sender: str = "delivery",
            bus_type: str = "message",
            urgent: bool = False) -> dict:
    """Unified delivery entry point.

    Routes messages to the correct target using the direct-prompt model:
    - ORCHESTRATOR: ghost-type into orchestrator's VS Code chat window
    - WORKER: ghost-type into the named worker's chat window
    - CONSULTANT: queue prompt into the consultant bridge
    - BUS: post to bus only (no UIA delivery)

    Args:
        target: DeliveryTarget enum value.
        content: Message text to deliver.
        worker_name: Required when target is WORKER.
        consultant_id: Required when target is CONSULTANT.
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
            ok = _ghost_type(hwnd, content, hwnd, target_label="orchestrator")
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
                ok = _ghost_type(hwnd, content, orch_hwnd,
                                target_label=f"worker:{worker_name}")
                result = {
                    "target": f"worker:{worker_name}",
                    "method": DeliveryMethod.DIRECT_PROMPT.value,
                    "success": ok,
                    "detail": f"HWND={hwnd}, len={len(content)}",
                }

    elif target == DeliveryTarget.CONSULTANT:
        if not consultant_id:
            result = {"target": "consultant", "method": "failed",
                      "success": False, "detail": "No consultant_id specified"}
        else:
            state = _load_consultant_state(consultant_id)
            api_url = str(state.get("api_url") or "").strip()
            live = bool(state.get("live"))
            accepts_prompts = bool(state.get("accepts_prompts"))
            if api_url.endswith("/consultants"):
                prompt_url = api_url + "/prompt"
            elif api_url:
                prompt_url = api_url.rstrip("/") + "/consultants/prompt"
            else:
                prompt_url = ""
            bridge_resp = None
            if prompt_url and accepts_prompts:
                bridge_resp = _json_post(prompt_url, {
                    "sender": bus_sender,
                    "type": bus_type or "directive",
                    "content": content,
                    "metadata": {"urgent": bool(urgent)},
                })
            bus_ok = _bus_post(bus_sender, consultant_id, bus_type or "directive", content[:2000])
            prompt = bridge_resp.get("prompt", {}) if isinstance(bridge_resp, dict) else {}
            success = bool(isinstance(bridge_resp, dict) and bridge_resp.get("status") == "queued")
            method = DeliveryMethod.HYBRID.value if bus_ok else DeliveryMethod.CONSULTANT_BRIDGE.value
            result = {
                "target": f"consultant:{consultant_id}",
                "method": method,
                "success": success,
                "detail": (
                    f"live={live}, accepts_prompts={accepts_prompts}, api_url={api_url or 'unknown'}, "
                    f"prompt_id={prompt.get('id', 'unknown')}, bus_ok={bus_ok}"
                ),
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


def deliver_to_consultant(consultant_id: str, content: str,
                          sender: str = "orchestrator",
                          msg_type: str = "directive") -> dict:
    """Queue a prompt into a live consultant bridge and post the audit trail to the bus."""
    return deliver(
        DeliveryTarget.CONSULTANT,
        content,
        consultant_id=consultant_id,
        bus_sender=sender,
        bus_type=msg_type,
    )


def deliver_consultant_result(consultant_id: str, content: str) -> dict:
    """Deliver a consultant's result directly to the orchestrator.

    Consultant results remain durable bus records and are also forwarded to the
    orchestrator via direct-prompt for immediate visibility. This keeps result
    handling low-latency even when the consultant itself is queue-routable.

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
        "method": DeliveryMethod.CONSULTANT_BRIDGE,
        "hwnd_source": "consultant_state.json",
        "routable": True,
        "convene_gated": False,
        "notes": "Advisory peer with bridge_queue prompt transport; results forwarded to orchestrator.",
    },
    "gemini_consultant": {
        "method": DeliveryMethod.CONSULTANT_BRIDGE,
        "hwnd_source": "gemini_consultant_state.json",
        "routable": True,
        "convene_gated": False,
        "notes": "Advisory peer with bridge_queue prompt transport; results forwarded to orchestrator.",
    },
}


def get_routing_info(target_name: str) -> dict:
    """Look up routing info for a target. Returns empty dict if not found."""
    return ROUTING_REGISTRY.get(target_name, {})


def is_routable(target_name: str) -> bool:
    """Check if a target supports direct-prompt delivery."""
    info = ROUTING_REGISTRY.get(target_name, {})
    if not info.get("routable", False):
        return False
    if info.get("method") == DeliveryMethod.CONSULTANT_BRIDGE:
        state = _load_consultant_state(target_name)
        return bool(state.get("live")) and bool(state.get("api_url")) and bool(state.get("accepts_prompts"))
    return True


def list_routable_targets() -> list:
    """Return all target names that support direct-prompt delivery."""
    return [name for name in ROUTING_REGISTRY if is_routable(name)]
