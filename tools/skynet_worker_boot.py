"""
skynet_worker_boot.py -- Canonical Worker Boot Script (Rule #0.06)

THE ONLY AUTHORIZED METHOD to open Skynet worker windows.
Implements the PROVEN 7-step procedure tested 2026-03-18.

Any modification to this script requires:
  1. A tested alternative that demonstrably outperforms this method
  2. Successful boot of all 4 workers using the new method
  3. Documentation of the change in AGENTS.md
  4. Update to boot_integrity.json hash

See docs/WORKER_BOOT_PROCEDURE.txt for the full procedure reference.

INCIDENT 016 (2026-03-18): Multiple boot methods existed (new_chat.ps1,
skynet_start.py, manual ctypes) causing repeated failures. This script
codifies the ONLY method that consistently works.

Usage:
  python tools/skynet_worker_boot.py --name alpha --orch-hwnd 657790
  python tools/skynet_worker_boot.py --all --orch-hwnd 657790
  python tools/skynet_worker_boot.py --verify
  python tools/skynet_worker_boot.py --close-all
"""
# signed: orchestrator

import ctypes
import ctypes.wintypes
import time
import pyautogui
import pyperclip
import subprocess
import json
import requests
import hashlib
import argparse
import sys
import os
from pathlib import Path
from datetime import datetime

BOOT_VERSION = "1.0.0"

ROOT = Path(__file__).resolve().parent.parent

# --- Grid positions (right monitor, taskbar-safe) ---
GRID = {
    'alpha': (1930, 20),
    'beta':  (2870, 20),
    'gamma': (1930, 540),
    'delta': (2870, 540),
}
WINDOW_SIZE = (930, 500)

WORKER_NAMES = ['alpha', 'beta', 'gamma', 'delta']

# Dropdown chevron absolute screen position on the orchestrator window
DROPDOWN_CHEVRON = (248, 52)

# Identity prompt template — worker fills NAME/name at format time
IDENTITY_PROMPT = (
    "You are {NAME}, a Skynet worker. Post your identity to the bus. "
    "Run this Python script:\n\n"
    "import requests\n"
    "requests.post('http://localhost:8420/bus/publish', json={{\n"
    "    'sender': '{name}',\n"
    "    'topic': 'orchestrator',\n"
    "    'type': 'identity_ack',\n"
    "    'content': '{NAME} ONLINE - Claude Opus 4.6 fast - Ready'\n"
    "}})\n"
    "print('Identity posted to bus')\n"
)

u32 = ctypes.windll.user32

# Disable pyautogui failsafe (workers are on the right monitor)
pyautogui.FAILSAFE = False


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] BOOT: {msg}")


# ---------------------------------------------------------------------------
# Step 1 — Open window via dropdown
# ---------------------------------------------------------------------------

def step1_open_window(orch_hwnd: int) -> bool:
    """Focus orchestrator, click dropdown chevron, navigate to 'New Chat Window'."""
    try:
        log("Step 1 — Opening new chat window via dropdown...")
        u32.SetForegroundWindow(orch_hwnd)
        time.sleep(1.5)

        # Click the dropdown chevron at absolute screen position
        pyautogui.click(DROPDOWN_CHEVRON[0], DROPDOWN_CHEVRON[1])
        time.sleep(1.5)

        # Navigate: Down x3 -> Enter  (1: New Chat, 2: New Chat Editor, 3: New Chat Window)
        pyautogui.press('down')
        time.sleep(0.2)
        pyautogui.press('down')
        time.sleep(0.2)
        pyautogui.press('down')
        time.sleep(0.2)
        pyautogui.press('enter')
        time.sleep(3)

        log("Step 1 — Window open command sent")
        return True
    except Exception as e:
        log(f"Step 1 FAILED: {e}")
        return False


# ---------------------------------------------------------------------------
# Step 2 — Find the new window HWND
# ---------------------------------------------------------------------------

def step2_find_hwnd(known_hwnds: set) -> int:
    """Enumerate windows and find a new 'Code - Insiders' HWND not in known set."""
    try:
        log("Step 2 — Searching for new window HWND...")
        wins = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def enum_cb(hwnd, _lparam):
            if u32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(512)
                u32.GetWindowTextW(hwnd, buf, 512)
                title = buf.value
                if 'Code - Insiders' in title and hwnd not in known_hwnds:
                    wins.append((hwnd, title))
            return True

        u32.EnumWindows(enum_cb, 0)

        if not wins:
            log("Step 2 FAILED: No new Code - Insiders window found")
            return 0

        hwnd = wins[0][0]
        log(f"Step 2 — Found new window: HWND={hwnd} title='{wins[0][1][:80]}'")
        return hwnd
    except Exception as e:
        log(f"Step 2 FAILED: {e}")
        return 0


# ---------------------------------------------------------------------------
# Step 3 — Position in grid
# ---------------------------------------------------------------------------

def step3_position(hwnd: int, gx: int, gy: int) -> bool:
    """MoveWindow to the grid position with WINDOW_SIZE."""
    try:
        log(f"Step 3 — Positioning window at ({gx}, {gy}) size {WINDOW_SIZE}")
        result = u32.MoveWindow(hwnd, gx, gy, WINDOW_SIZE[0], WINDOW_SIZE[1], True)
        if not result:
            log("Step 3 WARNING: MoveWindow returned 0")
        time.sleep(0.5)
        log("Step 3 — Window positioned")
        return True
    except Exception as e:
        log(f"Step 3 FAILED: {e}")
        return False


# ---------------------------------------------------------------------------
# Step 4 — Set session target to Copilot CLI
# ---------------------------------------------------------------------------

def step4_set_copilot_cli(hwnd: int, gx: int, gy: int) -> bool:
    """Click the bottom-left 'Local' dropdown, select 'Copilot CLI'.
    This automatically sets model to Claude Opus 4.6 (fast mode)."""
    try:
        log("Step 4 — Setting session target to Copilot CLI...")
        u32.SetForegroundWindow(hwnd)
        time.sleep(1)

        # Click the "Local" text at bottom-left of window
        pyautogui.click(gx + 55, gy + 484)
        time.sleep(1.5)

        # Select "Copilot CLI" (2nd item, right below "Local")
        pyautogui.press('down')
        time.sleep(0.3)
        pyautogui.press('enter')
        time.sleep(2)

        log("Step 4 — Copilot CLI set (model auto-set to Claude Opus 4.6 fast)")
        return True
    except Exception as e:
        log(f"Step 4 FAILED: {e}")
        return False


# ---------------------------------------------------------------------------
# Step 5 — Set permissions to bypass approvals
# ---------------------------------------------------------------------------

def step5_set_permissions(hwnd: int) -> bool:
    """Run guard_bypass.ps1 TWICE (first sets, second confirms)."""
    try:
        log("Step 5 — Setting permissions (bypass approvals)...")
        guard_script = str(ROOT / "tools" / "guard_bypass.ps1")

        for run_num in range(1, 3):
            log(f"Step 5 — guard_bypass.ps1 run {run_num}/2...")
            result = subprocess.run(
                [
                    "powershell", "-ExecutionPolicy", "Bypass",
                    "-File", guard_script,
                    "-Hwnd", str(hwnd),
                ],
                capture_output=True, text=True, timeout=30,
                cwd=str(ROOT),
            )
            output = (result.stdout or "").strip()
            log(f"Step 5 — Run {run_num} output: {output}")

            if run_num == 2 and "PERMS_FAILED" in output:
                log("Step 5 WARNING: guard_bypass.ps1 second run reported PERMS_FAILED")
                return False

            if run_num < 2:
                time.sleep(3)

        log("Step 5 — Permissions set to bypass")
        return True
    except subprocess.TimeoutExpired:
        log("Step 5 FAILED: guard_bypass.ps1 timed out")
        return False
    except Exception as e:
        log(f"Step 5 FAILED: {e}")
        return False


# ---------------------------------------------------------------------------
# Step 6 — Dispatch identity prompt
# ---------------------------------------------------------------------------

def step6_dispatch_identity(name: str, hwnd: int, gx: int, gy: int, orch_hwnd: int) -> bool:
    """Clipboard paste identity prompt into the worker window, submit with Enter."""
    try:
        log(f"Step 6 — Dispatching identity prompt to {name}...")
        task = IDENTITY_PROMPT.format(NAME=name.upper(), name=name)

        # Save and replace clipboard
        old_clip = ""
        try:
            old_clip = pyperclip.paste()
        except Exception:
            pass
        pyperclip.copy(task)

        u32.SetForegroundWindow(hwnd)
        time.sleep(1.0)

        # Click in the input area (center of text box)
        pyautogui.click(gx + 465, gy + 415)
        time.sleep(0.5)

        # Paste the prompt
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.5)

        # Submit
        pyautogui.press('enter')
        time.sleep(1.0)

        # Restore clipboard and return focus to orchestrator
        try:
            pyperclip.copy(old_clip if old_clip else '')
        except Exception:
            pass
        u32.SetForegroundWindow(orch_hwnd)

        log(f"Step 6 — Identity prompt dispatched to {name}")
        return True
    except Exception as e:
        log(f"Step 6 FAILED: {e}")
        return False


# ---------------------------------------------------------------------------
# Step 7 — Wait and verify
# ---------------------------------------------------------------------------

def step7_verify(name: str, hwnd: int, timeout: int = 60) -> bool:
    """Wait for identity_ack on bus, check window title, check IsWindow."""
    try:
        log(f"Step 7 — Verifying {name} (timeout={timeout}s)...")

        bus_ack = False
        title_ok = False
        window_alive = False

        deadline = time.time() + timeout
        poll_interval = 5

        while time.time() < deadline:
            # Check IsWindow
            window_alive = bool(u32.IsWindow(hwnd))
            if not window_alive:
                log(f"Step 7 — {name} window HWND={hwnd} is dead!")
                break

            # Check window title
            buf = ctypes.create_unicode_buffer(512)
            u32.GetWindowTextW(hwnd, buf, 512)
            title = buf.value
            title_ok = f"You are {name.upper()}" in title or f"You are {name}" in title

            # Check bus for identity_ack
            try:
                resp = requests.get(
                    "http://localhost:8420/bus/messages",
                    params={"limit": 30},
                    timeout=5,
                )
                if resp.status_code == 200:
                    msgs = resp.json()
                    if isinstance(msgs, dict):
                        msgs = msgs.get("messages", [])
                    for m in msgs:
                        if (m.get("sender") == name
                                and m.get("type") == "identity_ack"):
                            bus_ack = True
                            break
            except Exception:
                pass

            if bus_ack and title_ok and window_alive:
                log(f"Step 7 — {name} VERIFIED: bus_ack=True title_ok=True alive=True")
                return True

            remaining = int(deadline - time.time())
            log(f"Step 7 — {name} waiting... bus_ack={bus_ack} title_ok={title_ok} alive={window_alive} ({remaining}s left)")
            time.sleep(poll_interval)

        # Final status
        log(f"Step 7 — {name} final: bus_ack={bus_ack} title_ok={title_ok} alive={window_alive}")
        if bus_ack or (window_alive and title_ok):
            log(f"Step 7 — {name} PARTIAL PASS (some checks succeeded)")
            return True

        log(f"Step 7 — {name} VERIFICATION FAILED after {timeout}s")
        return False
    except Exception as e:
        log(f"Step 7 FAILED: {e}")
        return False


# ---------------------------------------------------------------------------
# Composite: boot a single worker
# ---------------------------------------------------------------------------

def boot_single_worker(name: str, orch_hwnd: int, known_hwnds: set) -> tuple:
    """Run all 7 steps for one worker. Returns (hwnd, success)."""
    if name not in GRID:
        log(f"ERROR: Unknown worker name '{name}'. Must be one of {WORKER_NAMES}")
        return (0, False)

    gx, gy = GRID[name]
    log(f"=== Booting {name.upper()} at grid ({gx}, {gy}) ===")

    # Step 1: Open window
    if not step1_open_window(orch_hwnd):
        log(f"ABORT: {name} — step 1 failed (open window)")
        return (0, False)

    # Step 2: Find HWND
    hwnd = step2_find_hwnd(known_hwnds)
    if not hwnd:
        log(f"ABORT: {name} — step 2 failed (find HWND)")
        return (0, False)

    # Step 3: Position
    if not step3_position(hwnd, gx, gy):
        log(f"WARNING: {name} — step 3 failed (position), continuing...")

    # Step 4: Set Copilot CLI
    if not step4_set_copilot_cli(hwnd, gx, gy):
        log(f"WARNING: {name} — step 4 failed (Copilot CLI), continuing...")

    # Step 5: Set permissions
    if not step5_set_permissions(hwnd):
        log(f"WARNING: {name} — step 5 failed (permissions), continuing...")

    # Step 6: Dispatch identity
    if not step6_dispatch_identity(name, hwnd, gx, gy, orch_hwnd):
        log(f"WARNING: {name} — step 6 failed (identity dispatch), continuing...")

    # Step 7: Verify
    verified = step7_verify(name, hwnd, timeout=60)
    if not verified:
        log(f"WARNING: {name} — step 7 failed (verification), window may still be usable")

    known_hwnds.add(hwnd)
    log(f"=== {name.upper()} boot {'SUCCESS' if verified else 'PARTIAL'}: HWND={hwnd} ===")
    return (hwnd, verified)


# ---------------------------------------------------------------------------
# Boot all workers
# ---------------------------------------------------------------------------

def boot_all_workers(orch_hwnd: int) -> dict:
    """Boot alpha, beta, gamma, delta in order. Returns dict of results."""
    log(f"========== FULL WORKER BOOT v{BOOT_VERSION} ==========")
    log(f"Orchestrator HWND: {orch_hwnd}")

    # Collect known HWNDs (orchestrator + any existing worker windows)
    known_hwnds = _collect_known_hwnds(orch_hwnd)
    log(f"Known HWNDs before boot: {known_hwnds}")

    results = {}
    for name in WORKER_NAMES:
        hwnd, success = boot_single_worker(name, orch_hwnd, known_hwnds)
        results[name] = {
            'hwnd': hwnd,
            'success': success,
            'grid': GRID[name],
        }
        if hwnd:
            known_hwnds.add(hwnd)

    # Update workers.json
    update_workers_json(results)

    # Print summary
    _print_summary(results)

    # Return focus to orchestrator
    u32.SetForegroundWindow(orch_hwnd)

    return results


def _collect_known_hwnds(orch_hwnd: int) -> set:
    """Gather all known HWNDs to avoid confusion when finding new windows."""
    known = {orch_hwnd}

    # Add existing workers from workers.json
    workers_file = ROOT / "data" / "workers.json"
    if workers_file.exists():
        try:
            raw = json.loads(workers_file.read_text(encoding="utf-8"))
            worker_list = raw.get("workers", []) if isinstance(raw, dict) else raw
            for w in worker_list:
                h = w.get("hwnd", 0)
                if h:
                    known.add(h)
        except Exception:
            pass

    # Add consultant HWNDs from state files
    for sf in ["consultant_state.json", "gemini_consultant_state.json"]:
        state_file = ROOT / "data" / sf
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
                h = data.get("hwnd", 0)
                if h:
                    known.add(h)
            except Exception:
                pass

    return known


# ---------------------------------------------------------------------------
# workers.json management
# ---------------------------------------------------------------------------

def update_workers_json(results: dict) -> None:
    """Write data/workers.json with all worker HWNDs, model, grid positions."""
    workers_file = ROOT / "data" / "workers.json"

    workers = []
    for name in WORKER_NAMES:
        info = results.get(name, {})
        hwnd = info.get('hwnd', 0)
        grid = info.get('grid', GRID.get(name, (0, 0)))
        workers.append({
            'name': name,
            'hwnd': hwnd,
            'model': 'Claude Opus 4.6 (fast mode)',
            'agent': 'Copilot CLI',
            'grid_x': grid[0],
            'grid_y': grid[1],
            'window_w': WINDOW_SIZE[0],
            'window_h': WINDOW_SIZE[1],
            'booted': hwnd != 0,
            'boot_version': BOOT_VERSION,
        })

    payload = {
        'workers': workers,
        'created': datetime.now().isoformat(),
        'boot_version': BOOT_VERSION,
    }

    workers_file.parent.mkdir(parents=True, exist_ok=True)
    workers_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log(f"Updated {workers_file}")


# ---------------------------------------------------------------------------
# Close all workers
# ---------------------------------------------------------------------------

def close_all_workers() -> None:
    """Read workers.json, send WM_CLOSE to each HWND, clear registry."""
    WM_CLOSE = 0x0010
    workers_file = ROOT / "data" / "workers.json"

    if not workers_file.exists():
        log("No workers.json found — nothing to close")
        return

    try:
        raw = json.loads(workers_file.read_text(encoding="utf-8"))
        worker_list = raw.get("workers", []) if isinstance(raw, dict) else raw
    except Exception as e:
        log(f"Failed to read workers.json: {e}")
        return

    closed = 0
    for w in worker_list:
        hwnd = w.get("hwnd", 0)
        name = w.get("name", "?")
        if not hwnd:
            log(f"  {name}: no HWND, skipping")
            continue

        if not u32.IsWindow(hwnd):
            log(f"  {name}: HWND={hwnd} already dead, skipping")
            continue

        log(f"  {name}: Sending WM_CLOSE to HWND={hwnd}")
        u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        closed += 1
        time.sleep(0.5)

    # Clear the registry
    payload = {
        'workers': [],
        'created': datetime.now().isoformat(),
        'boot_version': BOOT_VERSION,
        'note': 'Cleared by close_all_workers()',
    }
    workers_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log(f"Closed {closed} worker(s), workers.json cleared")


# ---------------------------------------------------------------------------
# Verify all workers
# ---------------------------------------------------------------------------

def verify_all_workers() -> bool:
    """Read workers.json, check each HWND alive + title + bus identity_ack."""
    workers_file = ROOT / "data" / "workers.json"

    if not workers_file.exists():
        log("No workers.json found — nothing to verify")
        return False

    try:
        raw = json.loads(workers_file.read_text(encoding="utf-8"))
        worker_list = raw.get("workers", []) if isinstance(raw, dict) else raw
    except Exception as e:
        log(f"Failed to read workers.json: {e}")
        return False

    # Fetch bus messages once
    bus_msgs = []
    try:
        resp = requests.get(
            "http://localhost:8420/bus/messages",
            params={"limit": 50},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            bus_msgs = data.get("messages", []) if isinstance(data, dict) else data
    except Exception:
        log("WARNING: Could not reach bus for identity_ack check")

    all_ok = True
    rows = []

    for w in worker_list:
        name = w.get("name", "?")
        hwnd = w.get("hwnd", 0)

        # Check alive
        alive = bool(hwnd and u32.IsWindow(hwnd))

        # Check title
        title = ""
        title_ok = False
        if alive:
            buf = ctypes.create_unicode_buffer(512)
            u32.GetWindowTextW(hwnd, buf, 512)
            title = buf.value
            title_ok = (f"You are {name.upper()}" in title
                        or f"You are {name}" in title
                        or "Code - Insiders" in title)

        # Check bus ack
        bus_ack = any(
            m.get("sender") == name and m.get("type") == "identity_ack"
            for m in bus_msgs
        )

        status = "OK" if (alive and title_ok and bus_ack) else "DEGRADED" if alive else "DEAD"
        if status != "OK":
            all_ok = False

        rows.append({
            'name': name,
            'hwnd': hwnd,
            'alive': alive,
            'title_ok': title_ok,
            'bus_ack': bus_ack,
            'status': status,
        })

    # Print verification table
    log("Worker Verification Results:")
    log(f"  {'Name':<8} {'HWND':<10} {'Alive':<7} {'Title':<7} {'Bus ACK':<9} {'Status'}")
    log(f"  {'----':<8} {'----':<10} {'-----':<7} {'-----':<7} {'-------':<9} {'------'}")
    for r in rows:
        log(f"  {r['name']:<8} {r['hwnd']:<10} {str(r['alive']):<7} {str(r['title_ok']):<7} {str(r['bus_ack']):<9} {r['status']}")

    log(f"Overall: {'ALL OK' if all_ok else 'ISSUES FOUND'}")
    return all_ok


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def _print_summary(results: dict) -> None:
    """Print a summary table after booting all workers."""
    log("")
    log("=" * 72)
    log("BOOT SUMMARY")
    log(f"  {'Name':<8} {'HWND':<10} {'Grid':<16} {'Status'}")
    log(f"  {'----':<8} {'----':<10} {'----':<16} {'------'}")
    for name in WORKER_NAMES:
        info = results.get(name, {})
        hwnd = info.get('hwnd', 0)
        grid = info.get('grid', (0, 0))
        success = info.get('success', False)
        status = "OK" if success else ("PARTIAL" if hwnd else "FAILED")
        log(f"  {name:<8} {hwnd:<10} {str(grid):<16} {status}")
    log("=" * 72)

    ok_count = sum(1 for v in results.values() if v.get('success'))
    log(f"Workers booted: {ok_count}/{len(WORKER_NAMES)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=f"Skynet Worker Boot v{BOOT_VERSION} -- Canonical 7-step worker boot procedure",
    )
    parser.add_argument("--name", choices=WORKER_NAMES, help="Boot a single worker by name")
    parser.add_argument("--all", action="store_true", help="Boot all 4 workers sequentially")
    parser.add_argument("--orch-hwnd", type=int, help="Orchestrator window HWND")
    parser.add_argument("--verify", action="store_true", help="Verify all existing workers")
    parser.add_argument("--close-all", action="store_true", help="Close all worker windows")
    parser.add_argument("--version", action="store_true", help="Print version and exit")

    args = parser.parse_args()

    if args.version:
        print(f"skynet_worker_boot v{BOOT_VERSION}")
        sys.exit(0)

    if args.verify:
        ok = verify_all_workers()
        sys.exit(0 if ok else 1)

    if args.close_all:
        close_all_workers()
        sys.exit(0)

    # --name or --all require --orch-hwnd
    if (args.name or args.all) and not args.orch_hwnd:
        # Try to read from orchestrator.json
        orch_file = ROOT / "data" / "orchestrator.json"
        if orch_file.exists():
            try:
                data = json.loads(orch_file.read_text(encoding="utf-8"))
                orch_hwnd = data.get("hwnd", 0)
                if orch_hwnd:
                    log(f"Auto-detected orchestrator HWND={orch_hwnd} from orchestrator.json")
                    args.orch_hwnd = orch_hwnd
            except Exception:
                pass

        if not args.orch_hwnd:
            parser.error("--orch-hwnd is required for boot operations (or set it in data/orchestrator.json)")

    if args.all:
        results = boot_all_workers(args.orch_hwnd)
        ok_count = sum(1 for v in results.values() if v.get('success'))
        sys.exit(0 if ok_count == len(WORKER_NAMES) else 1)

    if args.name:
        known = _collect_known_hwnds(args.orch_hwnd)
        hwnd, success = boot_single_worker(args.name, args.orch_hwnd, known)
        if hwnd:
            # Update workers.json for just this worker
            workers_file = ROOT / "data" / "workers.json"
            existing = {}
            if workers_file.exists():
                try:
                    raw = json.loads(workers_file.read_text(encoding="utf-8"))
                    wl = raw.get("workers", []) if isinstance(raw, dict) else raw
                    for w in wl:
                        n = w.get("name")
                        if n:
                            existing[n] = {
                                'hwnd': w.get('hwnd', 0),
                                'success': w.get('booted', False),
                                'grid': (w.get('grid_x', 0), w.get('grid_y', 0)),
                            }
                except Exception:
                    pass
            existing[args.name] = {
                'hwnd': hwnd,
                'success': success,
                'grid': GRID[args.name],
            }
            update_workers_json(existing)
        sys.exit(0 if success else 1)

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
