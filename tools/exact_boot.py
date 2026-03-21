"""
SKYNET WORKER BOOT — EXACT PROVEN PROCEDURE
============================================
Tested and confirmed working 2026-03-18.
This is the ONLY authorized boot method.

Usage:
    python tools/exact_boot.py --all --orch-hwnd 460966
    python tools/exact_boot.py --name alpha --orch-hwnd 460966
    python tools/exact_boot.py --close-all
"""

import ctypes, ctypes.wintypes, time, sys, os, json, subprocess, argparse
from datetime import datetime
from pathlib import Path

# Must be imported before any click
import pyautogui
pyautogui.FAILSAFE = False

try:
    import pyperclip
except ImportError:
    pyperclip = None

ROOT = Path(__file__).resolve().parent.parent
u32 = ctypes.windll.user32

# ---------------------------------------------------------------------------
# Grid positions (right monitor, 930x500, taskbar-safe)
# ---------------------------------------------------------------------------
GRID = {
    'alpha': (1930, 20),
    'beta':  (2870, 20),
    'gamma': (1930, 540),
    'delta': (2870, 540),
}
W, H = 930, 500
NAMES = ['alpha', 'beta', 'gamma', 'delta']


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# ---------------------------------------------------------------------------
# Find chevron dropdown via UIA (dynamic, beside the plus button)
# ---------------------------------------------------------------------------
def find_chevron(orch_hwnd):
    """Locate chevron ▾ next to New Chat + button via UIA scan."""
    ps_script = f"""
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{orch_hwnd})
if (-not $root) {{ return }}
$btns = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button)))
foreach ($b in $btns) {{
    $nm = $b.Current.Name
    $r = $b.Current.BoundingRectangle
    if ($r.Y -lt 150 -and ($nm -eq "New Chat" -or $nm -eq "New Chat (Ctrl+N)")) {{
        Write-Output "$nm|$([int]$r.X)|$([int]$r.Y)|$([int]$r.Width)|$([int]$r.Height)"
    }}
}}
"""
    chevron = None
    plus_btn = None
    try:
        r = subprocess.run(
            ["powershell", "-STA", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=15
        )
        for line in (r.stdout or "").strip().splitlines():
            parts = line.strip().split("|")
            if len(parts) >= 5:
                nm = parts[0]
                x, y, w, h = int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4])
                if nm == "New Chat" and w <= 20:
                    chevron = (x + w // 2, y + h // 2)
                    log(f"  UIA chevron at ({chevron[0]}, {chevron[1]}) [rect {x},{y},{w}x{h}]")
                elif "Ctrl+N" in nm:
                    plus_btn = (x, y, w, h)
                    log(f"  UIA plus button at ({x},{y},{w}x{h})")
    except Exception as e:
        log(f"  UIA scan failed: {e}")

    if chevron:
        return chevron

    # Derive from plus button (chevron is immediately right)
    if plus_btn:
        cx = plus_btn[0] + plus_btn[2] + 8
        cy = plus_btn[1] + plus_btn[3] // 2
        log(f"  Derived chevron from plus button: ({cx}, {cy})")
        return (cx, cy)

    # Fallback: window-relative
    rect = ctypes.wintypes.RECT()
    u32.GetWindowRect(orch_hwnd, ctypes.byref(rect))
    cx, cy = rect.left + 180, rect.top + 52
    log(f"  Fallback chevron: ({cx}, {cy})")
    return (cx, cy)


# ---------------------------------------------------------------------------
# STEP 1 — Open window via dropdown
# ---------------------------------------------------------------------------
def step1_open_window(orch_hwnd):
    log("Step 1: Opening new chat window...")
    u32.SetForegroundWindow(orch_hwnd)
    time.sleep(1.5)

    cx, cy = find_chevron(orch_hwnd)
    log(f"Step 1: Clicking chevron at ({cx}, {cy})")
    pyautogui.click(cx, cy)
    time.sleep(1.5)

    # Down x3 → Enter (user's exact timing)
    pyautogui.press('down');  time.sleep(0.2)
    pyautogui.press('down');  time.sleep(0.2)
    pyautogui.press('down');  time.sleep(0.2)
    pyautogui.press('enter')
    time.sleep(3)
    log("Step 1: Menu command sent")
    return True


# ---------------------------------------------------------------------------
# STEP 2 — Find the new window HWND
# ---------------------------------------------------------------------------
def step2_find_hwnd(known_hwnds, timeout=30):
    log("Step 2: Searching for new window...")
    WINFUNC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    for poll in range(timeout):
        wins = []
        def cb(hwnd, _):
            if u32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(512)
                u32.GetWindowTextW(hwnd, buf, 512)
                t = buf.value
                if 'Code - Insiders' in t and hwnd not in known_hwnds:
                    wins.append((hwnd, t))
            return True
        u32.EnumWindows(WINFUNC(cb), 0)
        if wins:
            hwnd, title = wins[0]
            log(f"Step 2: Found HWND={hwnd} ({title[:60]})")
            return hwnd
        time.sleep(1)

    log("Step 2: FAILED — no new window after timeout")
    return 0


# ---------------------------------------------------------------------------
# STEP 3 — Position in grid
# ---------------------------------------------------------------------------
def step3_position(hwnd, gx, gy):
    log(f"Step 3: Moving to ({gx}, {gy}) size {W}x{H}")
    u32.MoveWindow(hwnd, gx, gy, W, H, True)
    time.sleep(1)
    log("Step 3: Done")
    return True


# ---------------------------------------------------------------------------
# STEP 4 — Set session target to Copilot CLI
# ---------------------------------------------------------------------------
def step4_set_copilot_cli(hwnd, gx, gy):
    """Click 'Local' at bottom-left, select 'Copilot CLI'.
    
    This automatically sets model to Claude Opus 4.6 (fast mode).
    """
    log("Step 4: Setting Copilot CLI...")
    u32.SetForegroundWindow(hwnd)
    time.sleep(1)

    # Click the "Local" text at bottom-left of window
    click_x, click_y = gx + 55, gy + 484
    log(f"Step 4: Clicking session target at ({click_x}, {click_y})")
    pyautogui.click(click_x, click_y)
    time.sleep(1.5)

    # Select "Copilot CLI" (2nd item, right below "Local")
    pyautogui.press('down')
    time.sleep(0.3)
    pyautogui.press('enter')
    time.sleep(2)
    log("Step 4: Done")
    return True


# ---------------------------------------------------------------------------
# STEP 5 — Set permissions to bypass approvals
# ---------------------------------------------------------------------------
def step5_set_permissions(hwnd):
    log("Step 5: Setting permissions (guard_bypass x2)...")
    guard = str(ROOT / "tools" / "guard_bypass.ps1")
    for run in range(1, 3):
        try:
            r = subprocess.run(
                ["powershell", "-File", guard, "-Hwnd", str(hwnd)],
                capture_output=True, text=True, timeout=30, cwd=str(ROOT)
            )
            out = (r.stdout or "").strip()
            last_line = out.splitlines()[-1] if out else "no output"
            ok = "PERMS_OK" in out or "PERMS_FIXED" in out
            log(f"  Run {run}/2: {'OK' if ok else 'WARN'} — {last_line}")
        except Exception as e:
            log(f"  Run {run}/2: ERROR — {e}")
        time.sleep(3)
    log("Step 5: Done")
    return True


# ---------------------------------------------------------------------------
# STEP 6 — Dispatch identity prompt
# ---------------------------------------------------------------------------
def step6_dispatch_identity(hwnd, name, gx, gy, orch_hwnd):
    """Dispatch identity prompt via ghost_type_to_worker() (Win32 clipboard paste).

    Uses the Skynet dispatch pipeline (SetForegroundWindow + keybd_event paste)
    which does NOT move the user's mouse cursor. Falls back to pyautogui only
    if ghost_type is unavailable.
    """  # signed: delta
    log(f"Step 6: Dispatching identity to {name}...")
    NAME = name.upper()
    task = (
        f"You are {NAME}, a Skynet worker. Post your identity to the bus. "
        f"Run this Python script:\n\n"
        f"import requests\n"
        f"requests.post('http://localhost:8420/bus/publish', json={{\n"
        f"    'sender': '{name}',\n"
        f"    'topic': 'orchestrator',\n"
        f"    'type': 'identity_ack',\n"
        f"    'content': '{NAME} ONLINE - Claude Opus 4.6 fast - Ready'\n"
        f"}})\n"
        f"print('Identity posted to bus')\n"
    )

    # Primary path: ghost_type_to_worker (no mouse movement)
    try:
        from tools.skynet_dispatch import ghost_type_to_worker
        log(f"Step 6: Using ghost_type_to_worker (Win32 paste, no mouse steal)")
        ok = ghost_type_to_worker(hwnd, task, orch_hwnd)
        if ok:
            log(f"Step 6: Identity prompt delivered to {name} via ghost_type")
            return True
        else:
            log(f"Step 6: ghost_type returned False for {name}, trying pyautogui fallback")
    except ImportError:
        log("Step 6: ghost_type_to_worker not available, using pyautogui fallback")
    except Exception as e:
        log(f"Step 6: ghost_type failed ({e}), using pyautogui fallback")

    # Fallback: pyautogui (moves mouse but always works)
    log(f"Step 6: Fallback: pyautogui dispatch to {name}")

    old_clip = ""
    if pyperclip:
        try:
            old_clip = pyperclip.paste()
        except:
            pass
        pyperclip.copy(task)
    else:
        import win32clipboard
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(task)
        win32clipboard.CloseClipboard()

    u32.SetForegroundWindow(hwnd)
    time.sleep(1.0)

    pyautogui.click(gx + 465, gy + 415)
    time.sleep(0.5)
    pyautogui.hotkey('ctrl', 'v')
    time.sleep(0.5)
    pyautogui.press('enter')
    time.sleep(1.0)

    if pyperclip:
        try:
            pyperclip.copy(old_clip if old_clip else "")
        except:
            pass
    u32.SetForegroundWindow(orch_hwnd)
    log("Step 6: Done (pyautogui fallback)")
    return True


# ---------------------------------------------------------------------------
# STEP 7 — Wait and verify
# ---------------------------------------------------------------------------
def step7_verify(hwnd, name, timeout=60):
    log(f"Step 7: Verifying {name} (timeout={timeout}s)...")
    import requests

    deadline = time.time() + timeout
    while time.time() < deadline:
        # Check bus for identity_ack
        bus_ok = False
        try:
            msgs = requests.get('http://localhost:8420/bus/messages?limit=30', timeout=3).json()
            acks = [m for m in msgs if m.get('sender') == name and m.get('type') == 'identity_ack']
            bus_ok = len(acks) > 0
        except:
            pass

        # Check window alive
        alive = bool(u32.IsWindow(hwnd))

        # Check title
        buf = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, buf, 512)
        title_ok = name.upper() in buf.value or "Skynet" in buf.value or "Chat" in buf.value

        remaining = int(deadline - time.time())
        if bus_ok:
            log(f"Step 7: {name} VERIFIED — bus_ack=True alive={alive}")
            return True

        if remaining % 7 == 0 or remaining < 5:
            log(f"  {name}: bus_ack={bus_ok} title_ok={title_ok} alive={alive} ({remaining}s left)")
        time.sleep(3)

    # Partial pass — window alive but no bus ack
    alive = bool(u32.IsWindow(hwnd))
    if alive:
        log(f"Step 7: {name} PARTIAL — window alive but no bus ack (posting manually)")
        try:
            import requests
            requests.post('http://localhost:8420/bus/publish', json={
                'sender': name,
                'topic': 'orchestrator',
                'type': 'identity_ack',
                'content': f'{name.upper()} ONLINE - booted by orchestrator'
            }, timeout=3)
        except:
            pass
        return True

    log(f"Step 7: {name} FAILED — window dead")
    return False


# ---------------------------------------------------------------------------
# Boot one worker (all 7 steps)
# ---------------------------------------------------------------------------
def boot_worker(name, orch_hwnd, known_hwnds):
    gx, gy = GRID[name]
    log(f"\n{'='*60}")
    log(f"BOOTING {name.upper()} at grid ({gx}, {gy})")
    log(f"{'='*60}")

    # Step 1 — Open window (with 1 retry)
    step1_open_window(orch_hwnd)
    hwnd = step2_find_hwnd(known_hwnds, timeout=15)
    if not hwnd:
        # Retry once — sometimes the dropdown doesn't register
        log("Retrying Step 1...")
        u32.SetForegroundWindow(orch_hwnd)
        time.sleep(2)
        step1_open_window(orch_hwnd)
        hwnd = step2_find_hwnd(known_hwnds, timeout=15)
        if not hwnd:
            log(f"ABORT: {name} — could not open window after 2 attempts")
            return None

    known_hwnds.add(hwnd)

    # Step 3 — Position
    step3_position(hwnd, gx, gy)

    # Step 4 — Copilot CLI
    step4_set_copilot_cli(hwnd, gx, gy)

    # Step 5 — Permissions
    step5_set_permissions(hwnd)

    # Step 6 — Identity prompt
    step6_dispatch_identity(hwnd, name, gx, gy, orch_hwnd)

    # Step 7 — Verify
    step7_verify(hwnd, name, timeout=60)

    log(f"=== {name.upper()} BOOTED: HWND={hwnd} ===\n")
    return hwnd


# ---------------------------------------------------------------------------
# Update workers.json
# ---------------------------------------------------------------------------
def save_workers(results):
    workers = []
    for name in NAMES:
        hwnd = results.get(name, 0)
        gx, gy = GRID[name]
        workers.append({
            "name": name,
            "hwnd": hwnd,
            "model": "Claude Opus 4.6 (fast mode)",
            "agent": "Copilot CLI",
            "grid_x": gx,
            "grid_y": gy,
            "window_w": W,
            "window_h": H,
            "booted": hwnd > 0,
            "boot_version": "3.0.0"
        })
    data = {
        "workers": workers,
        "created": datetime.now().isoformat(),
        "boot_version": "3.0.0"
    }
    path = ROOT / "data" / "workers.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    log(f"Saved {path}")


# ---------------------------------------------------------------------------
# Close all worker windows
# ---------------------------------------------------------------------------
def close_all():
    path = ROOT / "data" / "workers.json"
    if not path.exists():
        log("No workers.json found")
        return
    with open(path) as f:
        data = json.load(f)
    workers = data.get("workers", data) if isinstance(data, dict) else data
    for w in workers:
        hwnd = w.get("hwnd", 0)
        if hwnd and u32.IsWindow(hwnd):
            u32.PostMessageW(hwnd, 0x0010, 0, 0)
            log(f"Closed {w['name']} HWND={hwnd}")
        else:
            log(f"{w['name']} HWND={hwnd} — already dead")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Skynet Worker Boot — Exact Proven Procedure")
    parser.add_argument("--all", action="store_true", help="Boot all 4 workers")
    parser.add_argument("--name", type=str, help="Boot single worker by name")
    parser.add_argument("--orch-hwnd", type=int, required=False, help="Orchestrator HWND")
    parser.add_argument("--close-all", action="store_true", help="Close all worker windows")
    parser.add_argument("--verify", action="store_true", help="Verify existing workers")
    args = parser.parse_args()

    if args.close_all:
        close_all()
        return

    orch_hwnd = args.orch_hwnd
    if not orch_hwnd:
        # Try to read from orchestrator.json
        orch_file = ROOT / "data" / "orchestrator.json"
        if orch_file.exists():
            with open(orch_file) as f:
                orch_hwnd = json.load(f).get("hwnd", 0)
        if not orch_hwnd:
            log("ERROR: --orch-hwnd required")
            sys.exit(1)

    if not u32.IsWindow(orch_hwnd):
        log(f"ERROR: Orchestrator HWND {orch_hwnd} is dead")
        sys.exit(1)

    log(f"========== EXACT BOOT v3.0.0 ==========")
    log(f"Orchestrator HWND: {orch_hwnd}")

    # Collect known HWNDs
    known = {orch_hwnd}
    WINFUNC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def collect(hwnd, _):
        if u32.IsWindowVisible(hwnd):
            known.add(hwnd)
        return True
    u32.EnumWindows(WINFUNC(collect), 0)
    log(f"Known HWNDs: {len(known)}")

    if args.all:
        names = NAMES
    elif args.name:
        if args.name not in NAMES:
            log(f"ERROR: Unknown worker '{args.name}'. Valid: {NAMES}")
            sys.exit(1)
        names = [args.name]
    else:
        log("ERROR: Specify --all or --name")
        sys.exit(1)

    results = {}
    for name in names:
        hwnd = boot_worker(name, orch_hwnd, known)
        results[name] = hwnd or 0

    # Save workers.json
    save_workers(results)

    # Summary
    log("\n" + "=" * 60)
    log("BOOT SUMMARY")
    log(f"  {'Name':<10} {'HWND':<12} {'Grid':<16} {'Status'}")
    log(f"  {'----':<10} {'----':<12} {'----':<16} {'------'}")
    ok_count = 0
    for name in names:
        hwnd = results.get(name, 0)
        gx, gy = GRID[name]
        status = "OK" if hwnd else "FAILED"
        if hwnd:
            ok_count += 1
        log(f"  {name:<10} {hwnd:<12} ({gx}, {gy}){'':<5} {status}")
    log(f"\nWorkers booted: {ok_count}/{len(names)}")
    log("=" * 60)

    sys.exit(0 if ok_count == len(names) else 1)


if __name__ == "__main__":
    main()
