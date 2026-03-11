#!/usr/bin/env python3
"""
UIA Engine — High-performance UI Automation scanner for Skynet.

Replaces per-call PowerShell spawning with in-process COM calls via comtypes.
Single OrCondition(Button|ListItem|Edit) scan returns full window state in ~30-50ms.

Usage:
    from tools.uia_engine import UIAEngine
    engine = UIAEngine()
    result = engine.scan(hwnd)         # single window
    results = engine.scan_all(hwnds)   # parallel scan all windows
    
    # result dict:
    {
        "state": "IDLE"|"PROCESSING"|"STEERING"|"TYPING"|"UNKNOWN",
        "model": "Pick Model, Claude Opus 4.6 (fast mode)",
        "agent": "Delegate Session - Copilot CLI",
        "has_cancel": False,
        "edit_value": "",
        "steering_count": 0,
        "element_count": 31,
        "scan_ms": 49.2,
        "model_ok": True,
        "agent_ok": True,
    }

Performance:
    COM UIA scan:     ~30-50ms per worker window, ~120ms for orchestrator
    PowerShell spawn: ~440ms per window (8-10x slower)
    Parallel 5 windows: ~200ms total

Architecture:
    - comtypes COM interface to IUIAutomation (in-process, no PowerShell)
    - OrCondition(Button|ListItem|Edit) — single FindAll gets everything
    - CacheRequest not used for FindAll (comtypes CachedName unreliable) — 
      CurrentName reads are fast enough at ~1ms each within same COM apartment
    - ThreadPoolExecutor for parallel multi-window scans
    - ValuePattern for Edit field content (TYPING detection)
    - BoundingRectangle for STEERING ListItem position filtering
"""

import ctypes
import ctypes.wintypes
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

# Lazy-init globals — comtypes requires CoInitialize per thread
_tls = threading.local()


def _get_uia():
    """Get or create a thread-local IUIAutomation instance."""
    if not hasattr(_tls, "uia") or _tls.uia is None:
        import comtypes
        import comtypes.client  # noqa: F401 — needed for type gen
        from comtypes.gen import UIAutomationClient as UIA_Mod

        _tls.UIA = UIA_Mod
        _tls.CLSID = comtypes.GUID("{ff48dba4-60ef-4201-aa87-54103eef594e}")

        # COM apartment: use MTA for thread safety
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass  # Already initialized in this thread

        _tls.uia = comtypes.CoCreateInstance(
            _tls.CLSID,
            interface=UIA_Mod.IUIAutomation,
            clsctx=comtypes.CLSCTX_INPROC_SERVER,
        )
    return _tls.uia, _tls.UIA


# ─── Property & Control Type IDs (from UIAutomationClient.h) ──────────────
UIA_ControlTypePropertyId = 30003
UIA_NamePropertyId = 30005
UIA_BoundingRectanglePropertyId = 30001
UIA_IsEnabledPropertyId = 30010
UIA_ClassNamePropertyId = 30012
UIA_AutomationIdPropertyId = 30011

# Control Type IDs
UIA_ButtonControlTypeId = 50000
UIA_EditControlTypeId = 50004
UIA_ListItemControlTypeId = 50007
UIA_CheckBoxControlTypeId = 50002
UIA_MenuItemControlTypeId = 50011
UIA_ComboBoxControlTypeId = 50003

# Pattern IDs
UIA_ValuePatternId = 10002
UIA_InvokePatternId = 10000
UIA_TogglePatternId = 10015
UIA_SelectionItemPatternId = 10010

# TreeScope
TreeScope_Element = 1
TreeScope_Children = 2
TreeScope_Descendants = 4
TreeScope_Subtree = 7  # Element | Children | Descendants


class WindowScan:
    """Result of scanning a single window via UIA."""

    __slots__ = (
        "hwnd", "state", "model", "agent", "has_cancel", "edit_value",
        "steering_count", "element_count", "scan_ms", "model_ok", "agent_ok",
        "error", "buttons", "list_items",
    )

    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self.state = "UNKNOWN"
        self.model = ""
        self.agent = ""
        self.has_cancel = False
        self.edit_value = ""
        self.steering_count = 0
        self.element_count = 0
        self.scan_ms = 0.0
        self.model_ok = False
        self.agent_ok = False
        self.error = ""
        self.buttons = []  # list of button names (for debugging)
        self.list_items = []  # list of (name, y_pos) tuples

    def to_dict(self) -> dict:
        return {
            "hwnd": self.hwnd,
            "state": self.state,
            "model": self.model,
            "agent": self.agent,
            "has_cancel": self.has_cancel,
            "edit_value": self.edit_value,
            "steering_count": self.steering_count,
            "element_count": self.element_count,
            "scan_ms": round(self.scan_ms, 1),
            "model_ok": self.model_ok,
            "agent_ok": self.agent_ok,
            "error": self.error,
        }

    def __repr__(self):
        return f"<WindowScan hwnd={self.hwnd} state={self.state} model_ok={self.model_ok} agent_ok={self.agent_ok} {self.scan_ms:.0f}ms>"


class UIAEngine:
    """High-performance UIA scanner using COM (comtypes).

    Thread-safe: each thread gets its own IUIAutomation COM instance.
    Reuse a single UIAEngine instance across your application.
    """

    def __init__(self):
        # Force type library generation on main thread
        _get_uia()

    def _build_condition(self):
        """Build OrCondition(Button|ListItem|Edit) — reusable per-thread."""
        uia, _ = _get_uia()
        bc = uia.CreatePropertyCondition(UIA_ControlTypePropertyId, UIA_ButtonControlTypeId)
        lc = uia.CreatePropertyCondition(UIA_ControlTypePropertyId, UIA_ListItemControlTypeId)
        ec = uia.CreatePropertyCondition(UIA_ControlTypePropertyId, UIA_EditControlTypeId)
        return uia.CreateOrCondition(bc, uia.CreateOrCondition(lc, ec))

    def _classify_elements(self, elements, n, result, bottom_threshold, UIA_Mod):
        """Classify scanned UIA elements into buttons, edits, list items."""
        for i in range(n):
            el = elements.GetElement(i)
            ct = el.CurrentControlType
            nm = el.CurrentName or ""

            if ct == UIA_ButtonControlTypeId:
                result.buttons.append(nm)
                if "Pick Model" in nm:
                    result.model = nm
                elif "Delegate Session" in nm:
                    result.agent = nm
                elif nm == "Cancel (Alt+Backspace)":
                    result.has_cancel = True

            elif ct == UIA_EditControlTypeId:
                if not result.edit_value:
                    try:
                        vp = el.GetCurrentPattern(UIA_ValuePatternId)
                        val_pattern = vp.QueryInterface(UIA_Mod.IUIAutomationValuePattern)
                        result.edit_value = val_pattern.CurrentValue or ""
                    except Exception:
                        pass

            elif ct == UIA_ListItemControlTypeId:
                if result.has_cancel and "STEERING" in nm.upper():
                    try:
                        li_rect = el.CurrentBoundingRectangle
                        if li_rect.top > bottom_threshold:
                            result.steering_count += 1
                            result.list_items.append((nm, li_rect.top))
                    except Exception:
                        result.steering_count += 1

    def scan(self, hwnd: int) -> WindowScan:
        """Scan a single window. Returns WindowScan with full state.

        Single COM call: FindAll(Descendants, OrCondition(Button|ListItem|Edit))
        Then iterates results to extract model, agent, cancel, edit, steering.
        ~30-50ms for a worker window, ~120ms for orchestrator (more elements).
        """
        result = WindowScan(hwnd)
        t0 = time.perf_counter()

        try:
            uia, UIA_Mod = _get_uia()
            cond = self._build_condition()

            root = uia.ElementFromHandle(ctypes.c_void_p(hwnd))
            if not root:
                result.error = "ElementFromHandle returned None"
                return result

            # Get window bounding rect for STEERING position filtering
            try:
                win_rect = root.CurrentBoundingRectangle
                win_top = win_rect.top
                win_height = win_rect.bottom - win_rect.top
                bottom_threshold = win_top + (win_height * 0.55)
            except Exception:
                bottom_threshold = 99999

            # Single FindAll — gets ALL buttons, list items, and edit controls
            elements = root.FindAll(TreeScope_Descendants, cond)
            n = elements.Length
            result.element_count = n

            self._classify_elements(elements, n, result, bottom_threshold, UIA_Mod)

            # Determine state
            if result.has_cancel and result.steering_count > 0:
                result.state = "STEERING"
            elif result.has_cancel:
                result.state = "PROCESSING"
            elif result.edit_value.strip():
                result.state = "TYPING"
            elif result.model:  # model button visible = window is responsive
                result.state = "IDLE"
            else:
                result.state = "UNKNOWN"

            # Model/agent correctness
            ml = result.model.lower()
            result.model_ok = "opus" in ml and "fast" in ml
            result.agent_ok = "copilot cli" in result.agent.lower()

        except Exception as e:
            result.error = str(e)
            result.state = "UNKNOWN"

        result.scan_ms = (time.perf_counter() - t0) * 1000
        return result

    def _scan_thread_safe(self, hwnd: int) -> WindowScan:
        """Thread-safe scan wrapper — initializes COM apartment in worker thread."""
        import comtypes
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass  # Already initialized
        try:
            return self.scan(hwnd)
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    def scan_all(self, hwnds: dict[str, int], max_workers: int = 5) -> dict[str, WindowScan]:
        """Parallel scan of multiple windows. Returns {name: WindowScan}.

        Args:
            hwnds: dict mapping name -> hwnd (e.g. {"alpha": 123, "orch": 456})
            max_workers: thread pool size (default 5 = 4 workers + 1 orchestrator)

        Uses MTA COM threading for safe parallel UIA access.
        """
        if len(hwnds) <= 1:
            # No point in threading for single window
            return {name: self.scan(hwnd) for name, hwnd in hwnds.items()}

        results = {}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._scan_thread_safe, hwnd): name for name, hwnd in hwnds.items()}
            for f in as_completed(futures):
                name = futures[f]
                try:
                    results[name] = f.result()
                except Exception as e:
                    ws = WindowScan(hwnds[name])
                    ws.error = str(e)
                    results[name] = ws
        return results

    def get_state(self, hwnd: int) -> str:
        """Quick state check — returns IDLE/PROCESSING/STEERING/TYPING/UNKNOWN."""
        return self.scan(hwnd).state

    def get_model(self, hwnd: int) -> str:
        """Quick model check — returns Pick Model button text."""
        return self.scan(hwnd).model

    def get_model_and_agent(self, hwnd: int) -> tuple[str, str]:
        """Quick model+agent check — returns (model_str, agent_str)."""
        r = self.scan(hwnd)
        return r.model, r.agent

    def is_correct(self, hwnd: int) -> tuple[bool, bool]:
        """Check if model and agent are correct. Returns (model_ok, agent_ok)."""
        r = self.scan(hwnd)
        return r.model_ok, r.agent_ok

    def wait_for_idle(self, hwnd: int, timeout: float = 60, poll: float = 0.5) -> bool:
        """Poll until window reaches IDLE state. Returns True if IDLE within timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.get_state(hwnd)
            if state == "IDLE":
                return True
            time.sleep(poll)
        return False

    def wait_for_state(self, hwnd: int, target: str, timeout: float = 60, poll: float = 0.5) -> bool:
        """Poll until window reaches target state."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.get_state(hwnd) == target:
                return True
            time.sleep(poll)
        return False

    # ─── Element interaction helpers ────────────────────────────────────────

    def find_button(self, hwnd: int, name_contains: str):
        """Find a specific button by partial name match. Returns the UIA element or None."""
        try:
            uia, _ = _get_uia()
            root = uia.ElementFromHandle(ctypes.c_void_p(hwnd))
            bc = uia.CreatePropertyCondition(UIA_ControlTypePropertyId, UIA_ButtonControlTypeId)
            elements = root.FindAll(TreeScope_Descendants, bc)
            for i in range(elements.Length):
                el = elements.GetElement(i)
                if name_contains in (el.CurrentName or ""):
                    return el
        except Exception:
            pass
        return None

    def invoke_button(self, hwnd: int, name_contains: str) -> bool:
        """Find and invoke (click) a button by partial name. Returns True if invoked."""
        el = self.find_button(hwnd, name_contains)
        if el:
            try:
                pattern = el.GetCurrentPattern(UIA_InvokePatternId)
                from comtypes.gen import UIAutomationClient as UIA_Mod
                invoke = pattern.QueryInterface(UIA_Mod.IUIAutomationInvokePattern)
                invoke.Invoke()
                return True
            except Exception:
                pass
        return False

    def cancel_generation(self, hwnd: int) -> bool:
        """Click the Cancel (Alt+Backspace) button. Returns True if clicked."""
        return self.invoke_button(hwnd, "Cancel (Alt+Backspace)")

    def get_edit_value(self, hwnd: int) -> str:
        """Read the current value of the Edit (input) control."""
        return self.scan(hwnd).edit_value


# ─── Module-level singleton for easy import ─────────────────────────────────
_engine: Optional[UIAEngine] = None
_lock = threading.Lock()


def get_engine() -> UIAEngine:
    """Get or create the global UIAEngine singleton."""
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = UIAEngine()
    return _engine


# ─── CLI for testing ────────────────────────────────────────────────────────
def _run_benchmark(engine, hwnds, workers):
    """Run UIA benchmark comparing COM vs PowerShell."""
    import subprocess

    print("=" * 70)
    print("UIA Engine Benchmark")
    print("=" * 70)

    t0 = time.perf_counter()
    for name, hwnd in hwnds.items():
        engine.scan(hwnd)
    seq_ms = (time.perf_counter() - t0) * 1000

    t2 = time.perf_counter()
    engine.scan_all(hwnds)
    par_ms = (time.perf_counter() - t2) * 1000

    ps = f"""Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$w=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{workers[0]['hwnd']})
$b=$w.FindAll([System.Windows.Automation.TreeScope]::Descendants,(New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Button)))
foreach($x in $b){{if($x.Current.Name -match 'Pick Model'){{Write-Output $x.Current.Name;break}}}}"""
    t4 = time.perf_counter()
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=10)
    ps_ms = (time.perf_counter() - t4) * 1000

    per_window_com = seq_ms / len(hwnds)
    speedup = ps_ms / per_window_com

    print(f"  COM sequential ({len(hwnds)} windows): {seq_ms:.0f}ms ({per_window_com:.0f}ms/window)")
    print(f"  COM parallel   ({len(hwnds)} windows): {par_ms:.0f}ms")
    print(f"  PowerShell     (1 window):            {ps_ms:.0f}ms")
    print(f"  Speedup: {speedup:.1f}x per window")
    print("=" * 70)


def main():
    import json
    import argparse

    parser = argparse.ArgumentParser(description="UIA Engine — scan windows via COM")
    parser.add_argument("--hwnd", type=int, help="Scan a specific HWND")
    parser.add_argument("--all", action="store_true", help="Scan all workers + orchestrator")
    parser.add_argument("--benchmark", action="store_true", help="Run benchmark comparison")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    engine = get_engine()

    if args.hwnd:
        r = engine.scan(args.hwnd)
        if args.json:
            print(json.dumps(r.to_dict(), indent=2))
        else:
            print(r)
            print(f"  model: {r.model}")
            print(f"  agent: {r.agent}")
            print(f"  state: {r.state}")
        return

    # Load workers
    import sys
    workers_file = "data/workers.json"
    try:
        with open(workers_file) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: {workers_file} not found")
        sys.exit(1)

    workers = data.get("workers", [])
    orch_hwnd = data.get("orchestrator_hwnd", 0)

    hwnds = {w["name"]: w["hwnd"] for w in workers}
    if orch_hwnd:
        hwnds["orchestrator"] = orch_hwnd

    if args.benchmark:
        _run_benchmark(engine, hwnds, workers)
        return

    # Default: scan all and display
    t0 = time.perf_counter()
    results = engine.scan_all(hwnds)
    t1 = time.perf_counter()

    if args.json:
        print(json.dumps({n: r.to_dict() for n, r in results.items()}, indent=2))
    else:
        print(f"UIA Engine scan ({len(results)} windows in {(t1-t0)*1000:.0f}ms):")
        for name in sorted(results.keys()):
            r = results[name]
            m_ok = "\u2705" if r.model_ok else "\u274c"
            a_ok = "\u2705" if r.agent_ok else "\u274c"
            print(f"  {name:12s} {r.state:12s} model={m_ok} agent={a_ok} ({r.element_count} els, {r.scan_ms:.0f}ms)")


if __name__ == "__main__":
    main()
