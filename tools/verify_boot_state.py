#!/usr/bin/env python3
"""Verify all worker windows have correct model, agent, and permissions.

Uses UIA scanning + mss screenshots for proof. Returns structured results.

Usage:
    python tools/verify_boot_state.py              # Verify all workers
    python tools/verify_boot_state.py --fix         # Verify and fix issues
    python tools/verify_boot_state.py --screenshot  # Save screenshots only

# signed: orchestrator
"""

import json
import sys
import time
import ctypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # Ensure repo root is in path for tool imports


def load_workers():
    wf = ROOT / "data" / "workers.json"
    raw = json.load(open(wf))
    return raw.get("workers", raw) if isinstance(raw, dict) else raw


def uia_scan_worker(hwnd):
    """Scan a worker window via UIA engine."""
    try:
        from tools.uia_engine import get_engine
        engine = get_engine()
        scan = engine.scan(hwnd)
        return {
            "state": scan.state,
            "model_ok": scan.model_ok,
            "agent_ok": scan.agent_ok,
            "model": scan.model if hasattr(scan, "model") else "unknown",
            "agent": scan.agent if hasattr(scan, "agent") else "unknown",
            "buttons": scan.buttons if hasattr(scan, "buttons") else [],
        }
    except Exception as e:
        return {"state": "ERROR", "model_ok": False, "agent_ok": False, "error": str(e)}


def check_permissions_uia(hwnd):
    """Check permission button text via UIA — uses the UIA engine scan."""
    try:
        from tools.uia_engine import get_engine
        engine = get_engine()
        scan = engine.scan(hwnd)
        for btn in (scan.buttons if hasattr(scan, "buttons") else []):
            if "Permissions" in btn or "Autopilot" in btn or "Approvals" in btn or "Bypass" in btn:
                return {
                    "button_text": btn,
                    "is_autopilot": "Autopilot" in btn or "Bypass" in btn,
                    "is_default": "Default" in btn,
                }
        return {"button_text": "NOT_FOUND", "is_autopilot": False, "is_default": False}
    except Exception as e:
        return {"button_text": f"SCAN_ERROR: {e}", "is_autopilot": False, "is_default": False}


def capture_worker_screenshot(name, hwnd):
    """Capture worker window screenshot via mss."""
    try:
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))

        import mss
        from PIL import Image

        region = {
            "left": rect.left,
            "top": rect.top,
            "width": rect.right - rect.left,
            "height": rect.bottom - rect.top,
        }
        ss_dir = ROOT / "data" / "dispatch_screenshots"
        ss_dir.mkdir(parents=True, exist_ok=True)
        ss_path = ss_dir / f"{name}_verify.png"

        with mss.mss() as sct:
            img = sct.grab(region)
            Image.frombytes("RGB", (img.width, img.height), img.rgb).save(str(ss_path))

        return str(ss_path)
    except Exception as e:
        return f"CAPTURE_ERROR: {e}"


def verify_all(fix=False, screenshot=False):
    """Verify all workers and return structured results."""
    import ctypes.wintypes  # needed for RECT

    workers = load_workers()
    user32 = ctypes.windll.user32
    results = {}

    for w in workers:
        name = w["name"]
        hwnd = w["hwnd"]

        # Check window alive
        is_alive = bool(user32.IsWindow(hwnd))
        is_visible = bool(user32.IsWindowVisible(hwnd))

        if not is_alive:
            results[name] = {
                "hwnd": hwnd, "alive": False, "visible": False,
                "status": "DEAD", "issues": ["Window does not exist"]
            }
            continue

        # UIA scan
        uia = uia_scan_worker(hwnd)

        # Permission check
        perms = check_permissions_uia(hwnd)

        # Screenshot
        ss_path = None
        if screenshot:
            ss_path = capture_worker_screenshot(name, hwnd)

        # Collect issues
        issues = []
        if not is_visible:
            issues.append("Window not visible")
        if not uia.get("model_ok", False):
            issues.append(f"Model incorrect: {uia.get('model', 'unknown')}")
        if not uia.get("agent_ok", False):
            issues.append(f"Agent incorrect: {uia.get('agent', 'unknown')}")
        if perms.get("is_default", False):
            issues.append(f"Permissions: DEFAULT (need Autopilot/Bypass)")

        status = "OK" if not issues else "ISSUES"

        results[name] = {
            "hwnd": hwnd,
            "alive": is_alive,
            "visible": is_visible,
            "state": uia.get("state", "UNKNOWN"),
            "model_ok": uia.get("model_ok", False),
            "agent_ok": uia.get("agent_ok", False),
            "permissions": perms.get("button_text", "unknown"),
            "perms_ok": perms.get("is_autopilot", False) or perms.get("button_text") == "NOT_FOUND",
            "status": status,
            "issues": issues,
            "screenshot": ss_path,
        }

        if fix and issues:
            print(f"  [{name}] Fixing {len(issues)} issues...")
            if not uia.get("model_ok", False):
                try:
                    from tools.skynet_start import guard_model
                    guard_model(hwnd)
                    print(f"  [{name}] Model guard executed")
                except Exception as e:
                    print(f"  [{name}] Model guard failed: {e}")

    return results


def print_results(results):
    """Print verification results in a formatted table."""
    print("\n" + "=" * 70)
    print("  SKYNET BOOT STATE VERIFICATION")
    print("=" * 70)

    all_ok = True
    for name, r in results.items():
        status = r.get("status", "UNKNOWN")
        icon = "✅" if status == "OK" else "❌" if status == "DEAD" else "⚠️"
        state = r.get("state", "?")
        model = "✅" if r.get("model_ok") else "❌"
        agent = "✅" if r.get("agent_ok") else "❌"

        print(f"  {icon} {name.upper():8s} HWND={r['hwnd']:8d}  "
              f"state={state:12s} model={model} agent={agent} "
              f"perms={'✅' if r.get('perms_ok') else '❌'}")

        if r.get("issues"):
            all_ok = False
            for issue in r["issues"]:
                print(f"     └─ ⚠️  {issue}")

        if r.get("screenshot"):
            print(f"     └─ 📸 {r['screenshot']}")

    print("=" * 70)
    if all_ok:
        print("  ✅ ALL WORKERS VERIFIED OK")
    else:
        print("  ❌ ISSUES DETECTED — see above")
    print("=" * 70 + "\n")
    return all_ok


if __name__ == "__main__":
    fix = "--fix" in sys.argv
    screenshot = "--screenshot" in sys.argv or "--ss" in sys.argv

    if not fix and not screenshot:
        screenshot = True  # default: verify with screenshots

    results = verify_all(fix=fix, screenshot=screenshot)
    ok = print_results(results)
    sys.exit(0 if ok else 1)
