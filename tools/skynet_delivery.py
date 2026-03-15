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
     unless marked urgent; elevated findings are batched into digest delivery
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
CRITICAL_PROCS_FILE = DATA_DIR / "critical_processes.json"
ORCH_LAYOUT_FILE = DATA_DIR / "orch_layout.json"
PREFIRE_SCREENSHOT_DIR = DATA_DIR / "delivery_prefire_screenshots"

# VS Code window title patterns (case-insensitive substring match)
_VSCODE_TITLE_MARKERS = ("Visual Studio Code", "VS Code")
_VSCODE_PROCESS_NAMES = ("Code - Insiders.exe", "Code.exe", "code")
_ORCH_TITLE_HINTS = (
    "skynet",
    "skynet-start",
    "skynet start",
    "orchestrator",
    "orchestrator-start",
    "orchestrator start",
    "orch-start",
    "orch start",
    "untitled-1",
)
_ORCH_SESSION_MARKERS = (
    "skynet-start",
    "skynet start",
    "orchestrator-start",
    "orchestrator start",
    "orch-start",
    "orch start",
)
_ORCH_IDENTITY_MARKERS = (
    "you are the skynet orchestrator",
    "skynet orchestrator live",
    "serving god",
    "ceo-level ai agent engaged",
    "god (user) --> orchestrator",
)
_ORCH_LEFT_PANE_MARKERS = (
    "orchestrator-start",
    "skynet-start",
    "orch-start",
    "skynet mass dispatch operation",
    "all 4 workers",
    "worker alpha",
    "worker beta",
    "worker gamma",
    "worker delta",
    "wait-all",
    "dispatch",
    "files changed",
    "apply",
)
_ORCH_LEFT_PANE_REJECT_MARKERS = (
    "gc-start",
    "cc-start",
    "gemini consultant",
    "codex consultant",
)
_ORCH_REJECT_TITLE_MARKERS = (
    "you are worker",
    "worker alpha",
    "worker beta",
    "worker gamma",
    "worker delta",
    "cc-start",
    "gc-start",
    "consultant",
)
_ORCH_REJECT_TEXT_MARKERS = _ORCH_REJECT_TITLE_MARKERS + (
    "gemini consultant",
    "codex consultant",
    "sender=consultant",
    "sender: consultant",
    "sender id: gemini_consultant",
    "you are the codex consultant",
    "you are the gemini consultant",
)


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


def _read_json_file(path: Path) -> dict:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _enum_vscode_hwnds() -> list[int]:
    """Enumerate visible VS Code top-level windows."""
    hwnds: list[int] = []
    try:
        enum_proc_type = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
        )

        def callback(hwnd, _lparam):
            try:
                hwnd_int = int(hwnd)
                if not _is_window(hwnd_int):
                    return True
                if not ctypes.windll.user32.IsWindowVisible(hwnd):
                    return True
                title = _get_window_title(hwnd_int)
                if title and any(marker.lower() in title.lower() for marker in _VSCODE_TITLE_MARKERS):
                    hwnds.append(hwnd_int)
            except Exception:
                pass
            return True

        callback_ptr = enum_proc_type(callback)
        ctypes.windll.user32.EnumWindows(callback_ptr, 0)
    except Exception:
        return []
    return hwnds


def _get_candidate_window_text(hwnd: int, max_items: int = 24, max_chars: int = 4000) -> str:
    """Extract recent visible conversation text for fingerprinting."""
    try:
        from skynet_realtime import _get_listitem_snapshot
    except Exception:
        try:
            from tools.skynet_realtime import _get_listitem_snapshot
        except Exception:
            return ""

    try:
        items = _get_listitem_snapshot(hwnd) or []
        if not items:
            return ""
        items.sort(key=lambda x: x[0])
        recent = [str(text).strip() for _, text in items[-max_items:] if str(text).strip()]
        return "\n".join(recent)[:max_chars]
    except Exception:
        return ""


def _get_window_scan_flags(hwnd: int) -> dict:
    """Return lightweight UIA model/agent signals for a candidate window."""
    try:
        from uia_engine import get_engine
    except Exception:
        try:
            from tools.uia_engine import get_engine
        except Exception:
            return {"agent": "", "model": "", "agent_ok": False, "model_ok": False}

    try:
        scan = get_engine().scan(hwnd)
        return {
            "agent": str(getattr(scan, "agent", "") or ""),
            "model": str(getattr(scan, "model", "") or ""),
            "agent_ok": bool(getattr(scan, "agent_ok", False)),
            "model_ok": bool(getattr(scan, "model_ok", False)),
        }
    except Exception:
        return {"agent": "", "model": "", "agent_ok": False, "model_ok": False}


_ORCH_PANE_EMPTY_RESULT = {
    "left_model": "",
    "left_agent": "",
    "left_model_ok": False,
    "left_agent_ok": False,
    "markers": [],
    "reject_markers": [],
}


def _scan_left_pane_elements(hwnd: int) -> tuple:
    """Enumerate UIA elements in the left pane band. Returns (left_model, left_agent, left_names)."""
    import comtypes  # signed: gamma (removed unused comtypes.client)
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
        return "", "", []

    try:
        win_rect = root.CurrentBoundingRectangle
        left_band_max_x = win_rect.left + min(320, int((win_rect.right - win_rect.left) * 0.33))
    except Exception:
        left_band_max_x = 320

    elements = root.FindAll(4, uia.CreateTrueCondition())
    left_names: list[str] = []
    left_model = ""
    left_agent = ""

    for i in range(elements.Length):
        el = elements.GetElement(i)
        try:
            name = str(el.CurrentName or "").strip()
            if not name:
                continue
            rect = el.CurrentBoundingRectangle
            if rect.left < 0 or rect.right > left_band_max_x:
                continue
            left_names.append(name)
            lowered = name.lower()
            if not left_model and lowered.startswith("pick model,"):
                left_model = name
            if not left_agent and lowered.startswith("delegate session"):
                left_agent = name
        except Exception:
            continue

    return left_model, left_agent, left_names


def _get_orchestrator_pane_signals(hwnd: int) -> dict:
    """Inspect the left pane of a shared VS Code window for orchestrator identity."""
    try:
        left_model, left_agent, left_names = _scan_left_pane_elements(hwnd)
    except Exception:
        return dict(_ORCH_PANE_EMPTY_RESULT)

    if not left_names:
        return dict(_ORCH_PANE_EMPTY_RESULT)

    haystack = "\n".join(left_names).lower()
    markers = [marker for marker in _ORCH_LEFT_PANE_MARKERS if marker in haystack]
    reject_markers = [marker for marker in _ORCH_LEFT_PANE_REJECT_MARKERS if marker in haystack]
    left_model_lower = left_model.lower()
    left_agent_lower = left_agent.lower()
    return {
        "left_model": left_model,
        "left_agent": left_agent,
        "left_model_ok": "opus" in left_model_lower and "fast" in left_model_lower,
        "left_agent_ok": "copilot cli" in left_agent_lower,
        "markers": markers,
        "reject_markers": reject_markers,
    }


def _enumerate_render_widgets(hwnd: int) -> list[int]:
    """Collect all Chrome render widgets under a VS Code top-level window."""  # signed: consultant
    widgets: list[int] = []
    user32 = ctypes.windll.user32

    def walk(parent: int) -> None:
        child = int(user32.FindWindowExW(parent, 0, None, None) or 0)
        while child:
            buf = ctypes.create_unicode_buffer(256)
            try:
                user32.GetClassNameW(child, buf, 256)
                if buf.value.startswith("Chrome_RenderWidgetHost"):
                    widgets.append(child)
            except Exception:
                pass
            walk(child)
            child = int(user32.FindWindowExW(parent, child, None, None) or 0)

    try:
        walk(int(hwnd))
    except Exception:
        return []
    return widgets


def _resolve_orchestrator_render_hwnd(hwnd: int) -> int:
    """Prefer the left-side render widget for shared orchestrator windows."""  # signed: consultant
    pane_signals = _get_orchestrator_pane_signals(hwnd)
    pane_override = bool(
        (pane_signals.get("left_agent_ok") and pane_signals.get("markers"))
        or (pane_signals.get("left_model_ok") and pane_signals.get("markers"))
        or (pane_signals.get("left_model_ok") and pane_signals.get("left_agent_ok"))
    )
    if not pane_override:
        return 0

    try:
        top_rect = ctypes.wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(top_rect)):
            return 0
        wnd_mid_x = top_rect.left + ((top_rect.right - top_rect.left) / 2.0)
    except Exception:
        return 0

    widgets = _enumerate_render_widgets(hwnd)
    if len(widgets) == 1:
        return int(widgets[0])

    best_hwnd = 0
    best_area = -1
    best_center = None
    for render_hwnd in widgets:
        try:
            rect = ctypes.wintypes.RECT()
            if not ctypes.windll.user32.GetWindowRect(render_hwnd, ctypes.byref(rect)):
                continue
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width <= 0 or height <= 0:
                continue
            center_x = rect.left + (width / 2.0)
            if center_x >= wnd_mid_x:
                continue
            area = width * height
            if area > best_area or (area == best_area and (best_center is None or center_x < best_center)):
                best_hwnd = int(render_hwnd)
                best_area = area
                best_center = center_x
        except Exception:
            continue
    return best_hwnd


def _focus_shared_orchestrator_pane(hwnd: int) -> bool:
    """Focus the left orchestrator pane in a shared VS Code window."""  # signed: consultant
    pane_signals = _get_orchestrator_pane_signals(hwnd)
    pane_override = bool(
        (pane_signals.get("left_agent_ok") and pane_signals.get("markers"))
        or (pane_signals.get("left_model_ok") and pane_signals.get("markers"))
        or (pane_signals.get("left_model_ok") and pane_signals.get("left_agent_ok"))
    )
    if not pane_override:
        return False

    screenshot_path = _capture_prefire_screenshot(hwnd, "orchestrator_focus")
    if not screenshot_path:
        return False

    try:
        import pyautogui

        rect = ctypes.wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return False

        focus_x = rect.left + int(width * 0.18)
        focus_y = rect.top + int(height * 0.93)
        pyautogui.click(focus_x, focus_y)
        time.sleep(0.2)
        return True
    except Exception:
        return False


def _score_title_and_content(title_lower: str, content_lower: str) -> tuple[int, bool]:
    """Score based on title/content marker matches. Returns (score, strong_signal)."""
    score = 0
    strong = False
    if any(marker in title_lower for marker in _ORCH_SESSION_MARKERS):
        score += 5
        strong = True
    if any(marker in title_lower for marker in _ORCH_IDENTITY_MARKERS):
        score += 4
        strong = True
    if any(marker in content_lower for marker in _ORCH_IDENTITY_MARKERS):
        score += 4
        strong = True
    if any(marker in content_lower for marker in _ORCH_SESSION_MARKERS):
        score += 2
        strong = True
    if any(hint in title_lower for hint in _ORCH_TITLE_HINTS):
        score += 1
    return score, strong


def _score_pane_and_scan(hwnd: int, pane_signals: dict, pane_override: bool) -> tuple[int, bool]:
    """Score based on pane signals and UIA scan flags. Returns (score, strong_signal)."""
    score = 0
    strong = False
    if pane_signals.get("left_model_ok"):
        score += 3
        strong = True
    if pane_signals.get("left_agent_ok"):
        score += 3
        strong = True
    if pane_signals.get("markers"):
        score += 4
        strong = True
    if pane_signals.get("reject_markers"):
        score -= 4

    scan_flags = _get_window_scan_flags(hwnd)
    agent_lower = str(scan_flags.get("agent") or "").lower()
    model_lower = str(scan_flags.get("model") or "").lower()
    if scan_flags.get("agent_ok"):
        score += 1
    if scan_flags.get("model_ok"):
        score += 1
    if "local" in agent_lower and not pane_override:
        score -= 4
    if "gemini" in model_lower and not pane_override:
        score -= 3
    return score, strong


def _score_orchestrator_candidate(hwnd: int, sources: set[str], boot_trigger: str = "") -> tuple[int, bool]:
    """Score how likely an HWND is the real orchestrator window."""
    validation = validate_hwnd(hwnd, "orchestrator")
    if not validation["valid"]:
        return -999, False

    title_lower = str(validation.get("title") or "").lower()
    content_lower = _get_candidate_window_text(hwnd).lower()
    pane_signals = _get_orchestrator_pane_signals(hwnd)
    pane_override = bool(
        (pane_signals.get("left_agent_ok") and pane_signals.get("markers"))
        or (pane_signals.get("left_model_ok") and pane_signals.get("markers"))
        or (pane_signals.get("left_model_ok") and pane_signals.get("left_agent_ok"))
    )

    if any(marker in title_lower for marker in _ORCH_REJECT_TITLE_MARKERS) and not pane_override:
        return -100, False
    if any(marker in content_lower for marker in _ORCH_REJECT_TEXT_MARKERS) and not pane_override:
        return -90, False

    score = 0
    strong_signal = False

    if len(sources) >= 2:
        score += 3
        strong_signal = True
    if boot_trigger in _ORCH_SESSION_MARKERS and "orchestrator.json" in sources:
        score += 1
        strong_signal = True

    tc_score, tc_strong = _score_title_and_content(title_lower, content_lower)
    score += tc_score
    strong_signal = strong_signal or tc_strong

    ps_score, ps_strong = _score_pane_and_scan(hwnd, pane_signals, pane_override)
    score += ps_score
    strong_signal = strong_signal or ps_strong

    return score, strong_signal


class HWNDValidationError(Exception):
    """Raised when HWND validation fails a security check."""
    pass


def _check_vscode_process(hwnd: int) -> tuple[int, str, bool]:
    """Check if the HWND belongs to a VS Code process. Returns (pid, proc_name, is_vscode)."""
    pid = _get_window_pid(hwnd)
    proc_name = _get_process_name(pid)
    is_vscode = bool(proc_name and any(proc_name.lower() == vsc.lower() for vsc in _VSCODE_PROCESS_NAMES))
    return pid, proc_name, is_vscode


def _check_vscode_title(hwnd: int) -> tuple[str, bool]:
    """Check if the HWND has a VS Code window title. Returns (title, has_marker)."""
    title = _get_window_title(hwnd)
    has_marker = bool(title and any(marker.lower() in title.lower() for marker in _VSCODE_TITLE_MARKERS))
    return title, has_marker


def validate_hwnd(hwnd: int, expected_target: str = "") -> dict:
    """Validate that an HWND is safe for ghost-type delivery.

    Returns dict with: valid, hwnd, pid, process_name, title, checks.
    """
    result = {
        "valid": False, "hwnd": hwnd, "pid": 0, "process_name": "",
        "title": "", "target": expected_target,
        "checks": {"nonzero": False, "is_window": False,
                    "is_vscode_process": False, "has_vscode_title": False},
    }

    if not hwnd:
        return result
    result["checks"]["nonzero"] = True

    if not _is_window(hwnd):
        return result
    result["checks"]["is_window"] = True

    pid, proc_name, is_vscode = _check_vscode_process(hwnd)
    result["pid"] = pid
    result["process_name"] = proc_name
    result["checks"]["is_vscode_process"] = is_vscode

    title, has_title = _check_vscode_title(hwnd)
    result["title"] = title
    result["checks"]["has_vscode_title"] = has_title

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


def _gather_orch_candidates() -> tuple[dict, set, str]:
    """Collect orchestrator HWND candidates from all truth sources.

    Returns (candidates: {hwnd: set_of_sources}, worker_hwnds, boot_trigger).
    """
    worker_hwnds: set = set()
    try:
        workers_data = json.loads(WORKERS_FILE.read_text(encoding="utf-8"))
        workers = workers_data if isinstance(workers_data, list) else workers_data.get("workers", [])
        if isinstance(workers, list):
            for worker in workers:
                hwnd = worker.get("hwnd") if isinstance(worker, dict) else None
                if isinstance(hwnd, int) and hwnd > 0:
                    worker_hwnds.add(hwnd)
    except Exception:
        pass

    candidates: dict[int, set[str]] = {}

    def add_candidate(hwnd: int, source: str) -> None:
        if not isinstance(hwnd, int) or hwnd <= 0:
            return
        candidates.setdefault(hwnd, set()).add(source)

    critical = _read_json_file(CRITICAL_PROCS_FILE)
    for proc in critical.get("processes", []):
        if not isinstance(proc, dict):
            continue
        if str(proc.get("role") or "").lower() == "orchestrator":
            add_candidate(int(proc.get("hwnd") or 0), "critical_processes.json")

    layout = _read_json_file(ORCH_LAYOUT_FILE)
    window = layout.get("window", {})
    if isinstance(window, dict):
        add_candidate(int(window.get("hwnd") or 0), "orch_layout.json")

    orch = _read_json_file(ORCH_FILE)
    boot_trigger = str(orch.get("boot_trigger") or "").strip().lower()
    add_candidate(int(orch.get("hwnd") or 0), "orchestrator.json")
    add_candidate(int(orch.get("orchestrator_hwnd") or 0), "orchestrator.json")
    for hwnd in _enum_vscode_hwnds():
        add_candidate(hwnd, "window_enum")

    return candidates, worker_hwnds, boot_trigger


def _load_orch_hwnd() -> int:
    """Resolve a validated orchestrator HWND from multiple truth sources."""
    candidates, worker_hwnds, boot_trigger = _gather_orch_candidates()

    best_hwnd = 0
    best_score = -999
    best_sources = 0

    for hwnd, sources in candidates.items():
        if hwnd in worker_hwnds or not _is_window(hwnd):
            continue
        score, strong_signal = _score_orchestrator_candidate(hwnd, sources, boot_trigger)
        if not strong_signal or score < 5:
            continue
        if score > best_score or (score == best_score and len(sources) > best_sources):
            best_hwnd = hwnd
            best_score = score
            best_sources = len(sources)

    return best_hwnd


def resolve_orchestrator_hwnd() -> int:
    """Public wrapper for the validated orchestrator HWND resolver."""
    return _load_orch_hwnd()


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


def _reserved_skynet_hwnds() -> set[int]:
    reserved: set[int] = set()
    try:
        workers_raw = json.loads(WORKERS_FILE.read_text(encoding="utf-8"))
        workers = workers_raw if isinstance(workers_raw, list) else workers_raw.get("workers", [])
        if isinstance(workers, list):
            for worker in workers:
                if not isinstance(worker, dict):
                    continue
                hwnd = int(worker.get("hwnd") or 0)
                if hwnd > 0:
                    reserved.add(hwnd)
    except Exception:
        pass

    try:
        orch = json.loads(ORCH_FILE.read_text(encoding="utf-8"))
        for key in ("orchestrator_hwnd", "hwnd"):
            hwnd = int(orch.get(key) or 0)
            if hwnd > 0:
                reserved.add(hwnd)
    except Exception:
        pass

    return reserved


def _consultant_hwnd_is_valid(state: dict, consultant_id: str) -> bool:
    try:
        hwnd = int(state.get("hwnd") or 0)
    except Exception:
        return False
    if hwnd <= 0 or hwnd in _reserved_skynet_hwnds() or not _is_window(hwnd):
        return False
    return bool(validate_hwnd(hwnd, f"consultant:{consultant_id}").get("valid"))  # signed: consultant


def _ghost_type(hwnd: int, text: str, orch_hwnd: int = 0,
                target_label: str = "", render_hwnd: int = 0) -> bool:
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

    screenshot_path = _capture_prefire_screenshot(hwnd, target_label)
    if not screenshot_path:
        _log_delivery(
            target_label or f"hwnd:{hwnd}",
            "blocked",
            False,
            0.0,
            "Prefire screenshot missing",
        )
        return False

    _log_delivery(
        target_label or f"hwnd:{hwnd}",
        "prefire_screenshot",
        True,
        0.0,
        screenshot_path,
    )

    try:
        from skynet_dispatch import ghost_type_to_worker
        return ghost_type_to_worker(hwnd, text, orch_hwnd or hwnd, render_hwnd=render_hwnd or None)
    except Exception:
        return False


def _bus_post(sender: str, topic: str, msg_type: str, content: str) -> bool:
    """Post a message to the Skynet bus via SpamGuard."""
    msg = {"sender": sender, "topic": topic, "type": msg_type, "content": content}
    try:
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish(msg)
        return result.get("allowed", False)
    except ImportError:
        # Fallback: shared.bus or direct HTTP (SpamGuard not available)
        try:
            from shared.bus import bus_post_fields
            return bus_post_fields(sender, topic, msg_type, content)
        except ImportError:
            import urllib.request
            try:
                payload = json.dumps(msg).encode("utf-8")
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
    # signed: gamma


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


def _sanitize_target_label(target_label: str, hwnd: int) -> str:
    raw = (target_label or f"hwnd_{hwnd}").strip().replace(":", "_")
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in raw)
    return (safe[:64] or f"hwnd_{hwnd}").strip("_") or f"hwnd_{hwnd}"


def _capture_prefire_screenshot(hwnd: int, target_label: str = "") -> str:
    """Capture a fresh screenshot artifact before any direct prompt is fired."""
    try:
        from tools.chrome_bridge.winctl import Desktop
    except Exception:
        try:
            from chrome_bridge.winctl import Desktop
        except Exception:
            return ""

    try:
        PREFIRE_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        target = _sanitize_target_label(target_label, hwnd)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = PREFIRE_SCREENSHOT_DIR / f"{target}_{stamp}.png"
        Desktop().screenshot(path=str(path), window=hwnd)

        existing = sorted(PREFIRE_SCREENSHOT_DIR.glob(f"{target}_*.png"))
        for old in existing[:-20]:
            old.unlink(missing_ok=True)
        return str(path)
    except Exception:
        return ""


def _deliver_to_orch_hwnd(content: str) -> dict:
    """Handle ORCHESTRATOR target delivery."""
    hwnd = _load_orch_hwnd()
    if not hwnd:
        return {"target": "orchestrator", "method": "failed",
                "success": False, "detail": "No orchestrator HWND"}
    render_hwnd = _resolve_orchestrator_render_hwnd(hwnd)
    focused_left_pane = _focus_shared_orchestrator_pane(hwnd)
    ok = _ghost_type(hwnd, content, hwnd, target_label="orchestrator", render_hwnd=render_hwnd)
    return {
        "target": "orchestrator",
        "method": DeliveryMethod.DIRECT_PROMPT.value,
        "success": ok,
        "detail": f"HWND={hwnd}, render={render_hwnd or 0}, focused_left_pane={focused_left_pane}, len={len(content)}",
    }


def _deliver_to_worker_hwnd(content: str, worker_name: Optional[str]) -> dict:
    """Handle WORKER target delivery."""
    if not worker_name:
        return {"target": "worker", "method": "failed",
                "success": False, "detail": "No worker_name specified"}
    hwnd = _load_worker_hwnd(worker_name)
    orch_hwnd = _load_orch_hwnd()
    if not hwnd:
        return {"target": f"worker:{worker_name}", "method": "failed",
                "success": False, "detail": f"No HWND for {worker_name}"}
    ok = _ghost_type(hwnd, content, orch_hwnd, target_label=f"worker:{worker_name}")
    return {
        "target": f"worker:{worker_name}",
        "method": DeliveryMethod.DIRECT_PROMPT.value,
        "success": ok,
        "detail": f"HWND={hwnd}, len={len(content)}",
    }


def _deliver_to_consultant_ghost_type(content: str, consultant_id: str) -> dict:
    """Attempt ghost_type delivery to consultant window via HWND.

    Loads consultant HWND from state file, validates it, and ghost-types.
    Returns result dict with delivery_status='delivered' on success,
    'failed' if no HWND or ghost_type fails.
    """  # signed: gamma
    state = _load_consultant_state(consultant_id)
    hwnd = state.get("hwnd", 0)
    if not _consultant_hwnd_is_valid(state, consultant_id):
        return {
            "target": f"consultant:{consultant_id}",
            "method": "ghost_type",
            "success": False,
            "delivery_status": "failed",
            "detail": f"No truthful consultant HWND in state file for {consultant_id}",
        }
    hwnd = int(hwnd)
    orch_hwnd = _load_orch_hwnd()
    ok = _ghost_type(hwnd, content, orch_hwnd or hwnd,
                     target_label=f"consultant:{consultant_id}")
    return {
        "target": f"consultant:{consultant_id}",
        "method": DeliveryMethod.DIRECT_PROMPT.value,
        "success": ok,
        "delivery_status": "delivered" if ok else "failed",
        "detail": f"HWND={hwnd}, len={len(content)}, ghost_type={'ok' if ok else 'failed'}",
    }  # signed: gamma


def _deliver_to_consultant_bridge(content: str, consultant_id: Optional[str],
                                  bus_sender: str, bus_type: str,
                                  urgent: bool) -> dict:
    """Handle CONSULTANT target delivery: ghost_type primary, bridge fallback.

    Delivery priority:
    1. ghost_type via HWND (if consultant has HWND) -> delivery_status='delivered'
    2. bridge_queue via HTTP POST (fallback) -> delivery_status='queued'
    3. bus audit trail always posted regardless of delivery method

    TRUTH: success=True ONLY for ghost_type 'delivered'. queued != delivered.
    """  # signed: gamma
    if not consultant_id:
        return {"target": "consultant", "method": "failed",
                "success": False, "detail": "No consultant_id specified"}

    # --- Phase 1: Try ghost_type as primary delivery --- signed: gamma
    ghost_result = _deliver_to_consultant_ghost_type(content, consultant_id)
    ghost_ok = ghost_result.get("success", False)

    # --- Phase 2: Bridge queue as fallback + audit trail --- signed: gamma
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
    # Bus audit trail always posted
    bus_ok = _bus_post(bus_sender, consultant_id, bus_type or "directive", content[:2000])
    prompt = bridge_resp.get("prompt", {}) if isinstance(bridge_resp, dict) else {}
    bridge_status = bridge_resp.get("status", "") if isinstance(bridge_resp, dict) else ""

    # --- Phase 3: Determine final delivery status --- signed: gamma
    # TRUTH PRINCIPLE: queued != delivered. success=True ONLY for real delivery.
    if ghost_ok:
        delivery_status = "delivered"
        method = DeliveryMethod.DIRECT_PROMPT.value
        success = True
    elif bridge_status == "queued":
        delivery_status = "queued"
        method = DeliveryMethod.CONSULTANT_BRIDGE.value
        success = False  # queued is NOT delivered -- TRUTH PRINCIPLE
    elif bridge_status in ("delivered", "consumed"):
        delivery_status = bridge_status
        method = DeliveryMethod.CONSULTANT_BRIDGE.value
        success = True
    else:
        delivery_status = "failed"
        method = "failed"
        success = False

    if bus_ok and method != "failed":
        method = DeliveryMethod.HYBRID.value

    return {
        "target": f"consultant:{consultant_id}",
        "method": method,
        "success": success,
        "delivery_status": delivery_status,
        "detail": (
            f"ghost_type={'delivered' if ghost_ok else 'no_hwnd_or_failed'}, "
            f"live={live}, accepts_prompts={accepts_prompts}, api_url={api_url or 'unknown'}, "
            f"prompt_id={prompt.get('id', 'unknown')}, bus_ok={bus_ok}, "
            f"bridge_status={bridge_status}, delivery_status={delivery_status}"
        ),
    }  # signed: gamma


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
    - CONSULTANT: ghost_type primary (if HWND), bridge_queue fallback + bus audit
    - BUS: post to bus only (no UIA delivery)
    """
    t0 = time.time()

    if target == DeliveryTarget.ORCHESTRATOR:
        result = _deliver_to_orch_hwnd(content)
    elif target == DeliveryTarget.WORKER:
        result = _deliver_to_worker_hwnd(content, worker_name)
    elif target == DeliveryTarget.CONSULTANT:
        result = _deliver_to_consultant_bridge(
            content, consultant_id, bus_sender, bus_type, urgent)
    elif target == DeliveryTarget.BUS:
        topic = bus_topic or "general"
        ok = _bus_post(bus_sender, topic, bus_type, content)
        result = {"target": f"bus:{topic}", "method": DeliveryMethod.BUS_POST.value,
                  "success": ok, "detail": f"topic={topic}, type={bus_type}"}
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


def deliver_self_invoke(worker_name: str, task_content: str,
                        sender: str = "idle_monitor") -> dict:
    """Deliver a self-invoke task directly to a worker's chat window.

    Used by the idle monitor to wake up idle workers with pending work.
    Ghost-types the task into the worker's VS Code chat, posts a bus audit
    record, and logs the delivery with delivery_type='self_invoke'.

    Args:
        worker_name: Target worker name (e.g. 'alpha', 'beta').
        task_content: The task/prompt text to type into the worker window.
        sender: Bus sender identity for the audit record.

    Returns:
        dict with target, method, success, detail, latency_ms keys.
    """
    t0 = time.time()

    hwnd = _load_worker_hwnd(worker_name)
    if not hwnd:
        result = {
            "target": f"worker:{worker_name}",
            "method": "failed",
            "success": False,
            "detail": f"No HWND found for worker '{worker_name}' in workers.json",
        }
        latency_ms = (time.time() - t0) * 1000
        result["latency_ms"] = round(latency_ms, 1)
        _log_delivery(result["target"], result["method"], result["success"],
                      latency_ms, result["detail"])
        return result

    orch_hwnd = _load_orch_hwnd()
    ok = _ghost_type(hwnd, task_content, orch_hwnd,
                     target_label=f"worker:{worker_name}")

    # Bus audit record
    _bus_post(sender, "workers", "self_invoke",
              f"Delivered self-invoke to {worker_name}")

    latency_ms = (time.time() - t0) * 1000
    result = {
        "target": f"worker:{worker_name}",
        "method": DeliveryMethod.DIRECT_PROMPT.value,
        "success": ok,
        "detail": f"HWND={hwnd}, len={len(task_content)}, sender={sender}",
        "latency_ms": round(latency_ms, 1),
    }

    # Log with self_invoke delivery type
    _log_delivery(result["target"], "self_invoke", result["success"],
                  latency_ms, result["detail"])
    return result
    # signed: beta


def pull_pending_work(worker_name: str) -> Optional[str]:
    """Pull the highest-priority pending task for a specific worker.

    Checks two sources:
      1. Bus messages: topic='workers' with metadata.target matching worker_name,
         or type='directive' with route matching worker_name.
      2. data/todos.json: pending items assigned to worker_name.

    Returns the highest-priority task content string, or None if nothing pending.

    Args:
        worker_name: The worker name to check for pending work.

    Returns:
        Task content string or None.
    """
    candidates = []  # list of (priority, content) tuples

    # --- Source 1: Bus messages ---
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://localhost:8420/bus/messages?limit=30",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            messages = data if isinstance(data, list) else data.get("messages", [])
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                topic = msg.get("topic", "")
                msg_type = msg.get("type", "")
                content = msg.get("content", "")
                metadata = msg.get("metadata") or {}
                route = msg.get("route", "")

                # Match: topic=workers + metadata.target=worker_name
                if (topic == "workers"
                        and str(metadata.get("target", "")).lower() == worker_name.lower()
                        and content):
                    candidates.append((1, content))

                # Match: type=directive + route=worker_name
                if (msg_type == "directive"
                        and str(route).lower() == worker_name.lower()
                        and content):
                    candidates.append((0, content))  # directives are highest priority
    except Exception:
        pass  # Bus unavailable -- fall through to todos

    # --- Source 2: data/todos.json ---
    try:
        todos_file = DATA_DIR / "todos.json"
        if todos_file.exists():
            with open(todos_file, "r", encoding="utf-8") as f:
                todos = json.load(f)
            if isinstance(todos, dict):
                todos = todos.get("todos", [])  # signed: beta
            if isinstance(todos, list):
                for item in todos:
                    if not isinstance(item, dict):
                        continue
                    status = str(item.get("status", "")).lower()
                    assignee = str(item.get("assignee", "")).lower()
                    if status == "pending" and assignee == worker_name.lower():
                        title = item.get("title", item.get("task", ""))
                        priority = item.get("priority", 5)
                        # Lower priority number = higher priority
                        try:
                            pri_val = int(priority)
                        except (ValueError, TypeError):
                            pri_val = 5
                        candidates.append((pri_val, str(title)))
    except Exception:
        pass  # todos.json unavailable or malformed

    if not candidates:
        return None

    # Sort by priority (lower number = higher priority), return first
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1] or None
    # signed: beta


def _format_elevated_digest(entries: list[dict], window_seconds: int = 1800) -> tuple[str, str]:
    """Build the durable bus summary and the direct-prompt digest body."""
    window_minutes = max(1, int(window_seconds // 60))
    stamp = datetime.now().isoformat(timespec="seconds")
    summary_lines = [
        f"[CONVENE-DELIVERY] type=elevated_digest window={window_minutes}m "
        f"count={len(entries)} generated_at={stamp}"
    ]
    prompt_lines = summary_lines + [""]

    for idx, entry in enumerate(entries, start=1):
        report = " ".join(str(entry.get("report") or "").split())
        voters = list(entry.get("voters") or [])
        vote_total = int(entry.get("vote_total") or len(voters) or 0)
        vote_count = int(entry.get("vote_count") or len(voters))
        summary_lines.append(
            f"{idx}. gate={entry.get('gate_id', '-')}"
            f" proposer={entry.get('proposer', 'unknown')}"
            f" votes={vote_count}/{max(vote_total, vote_count)}"
            f" repeats={int(entry.get('repeat_count') or 1)}"
            f" :: {report[:220]}"
        )
        prompt_lines.append(
            f"{idx}. gate={entry.get('gate_id', '-')}"
            f" proposer={entry.get('proposer', 'unknown')}"
            f" voters={','.join(voters) or '-'}"
            f" repeats={int(entry.get('repeat_count') or 1)}"
        )
        prompt_lines.append(f"   {report[:320]}")

    return "\n".join(summary_lines)[:4000], "\n".join(prompt_lines)[:4000]


def deliver_elevated_digest(entries: list[dict], window_seconds: int = 1800) -> dict:
    """Deliver a consolidated convene digest to the orchestrator."""
    if not entries:
        return {
            "target": "orchestrator",
            "method": DeliveryMethod.HYBRID.value,
            "success": True,
            "detail": "No elevated digest entries",
            "delivery_type": "elevated_digest",
            "count": 0,
        }

    bus_content, formatted = _format_elevated_digest(entries, window_seconds)
    _bus_post("convene-gate", "orchestrator", "elevated_digest", bus_content)
    result = deliver(DeliveryTarget.ORCHESTRATOR, formatted)
    result["delivery_type"] = "elevated_digest"
    result["count"] = len(entries)
    return result


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
        "method": DeliveryMethod.DIRECT_PROMPT,
        "fallback": DeliveryMethod.CONSULTANT_BRIDGE,
        "hwnd_source": "consultant_state.json",
        "routable": True,
        "convene_gated": False,
        "notes": "Ghost_type primary (if HWND), bridge_queue fallback; queued != delivered.",
    },  # signed: gamma
    "gemini_consultant": {
        "method": DeliveryMethod.DIRECT_PROMPT,
        "fallback": DeliveryMethod.CONSULTANT_BRIDGE,
        "hwnd_source": "gemini_consultant_state.json",
        "routable": True,
        "convene_gated": False,
        "notes": "Ghost_type primary (if HWND), bridge_queue fallback; queued != delivered.",
    },  # signed: gamma
}


def get_routing_info(target_name: str) -> dict:
    """Look up routing info for a target. Returns empty dict if not found."""
    return ROUTING_REGISTRY.get(target_name, {})


def is_routable(target_name: str) -> bool:
    """Check if a target supports direct-prompt delivery."""
    info = ROUTING_REGISTRY.get(target_name, {})
    if not info.get("routable", False):
        return False
    # Consultant targets: routable if HWND exists (ghost_type) OR bridge is live
    fallback = info.get("fallback")
    if fallback == DeliveryMethod.CONSULTANT_BRIDGE:
        state = _load_consultant_state(target_name)
        has_hwnd = _consultant_hwnd_is_valid(state, target_name)
        bridge_live = (bool(state.get("live")) and bool(state.get("api_url"))
                       and bool(state.get("accepts_prompts")))
        return has_hwnd or bridge_live  # signed: gamma
    if info.get("method") == DeliveryMethod.CONSULTANT_BRIDGE:
        state = _load_consultant_state(target_name)
        return bool(state.get("live")) and bool(state.get("api_url")) and bool(state.get("accepts_prompts"))
    return True  # signed: gamma


def list_routable_targets() -> list:
    """Return all target names that support direct-prompt delivery."""
    return [name for name in ROUTING_REGISTRY if is_routable(name)]
