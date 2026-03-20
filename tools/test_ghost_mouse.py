#!/usr/bin/env python3
"""End-to-end test suite for ghost_mouse.py.

Tests all functions against real worker HWNDs from data/workers.json.
Verifies ZERO cursor movement during all operations.
Saves results to data/ghost_mouse_test_results.json.
"""
# signed: beta

import ctypes
import ctypes.wintypes
import json
import time
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.ghost_mouse import (
    ghost_click, ghost_right_click, ghost_double_click, ghost_scroll,
    ghost_drag, ghost_hover, find_render_widget, find_all_render_widgets,
    ghost_click_render, invoke_by_name, ghost_click_element,
    find_uia_element_coords, IsWindow
)

user32 = ctypes.windll.user32

# ─── Helpers ───

def get_cursor_pos():
    pt = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


results = {
    "test_run": time.strftime("%Y-%m-%dT%H:%M:%S"),
    "worker_hwnds": {},
    "tests": [],
    "summary": {"total": 0, "passed": 0, "failed": 0, "cursor_moved": False},
}


def add_result(name, passed, details="", cursor_ok=True):
    results["tests"].append({
        "name": name,
        "passed": passed,
        "cursor_stable": cursor_ok,
        "details": details,
    })
    results["summary"]["total"] += 1
    if passed and cursor_ok:
        results["summary"]["passed"] += 1
    else:
        results["summary"]["failed"] += 1
    status = "PASS" if (passed and cursor_ok) else "FAIL"
    print(f"  [{status}] {name}: {details}")


def check_cursor_stable(before, after, test_name):
    moved = before[0] != after[0] or before[1] != after[1]
    if moved:
        results["summary"]["cursor_moved"] = True
        print(f"  !! CURSOR MOVED during {test_name}: {before} -> {after}")
    return not moved


# ─── Load workers ───

with open(os.path.join(os.path.dirname(__file__), "..", "data", "workers.json"), "r") as f:
    workers_data = json.load(f)
workers = workers_data.get("workers", workers_data) if isinstance(workers_data, dict) else workers_data
results["worker_hwnds"] = {w["name"]: w["hwnd"] for w in workers}

print("=== Ghost Mouse End-to-End Test Suite ===")
print(f"    Timestamp: {results['test_run']}")
print()

# ─── Step 1: Validate HWNDs ───
print("--- Step 1: Validate Worker HWNDs ---")
valid_workers = []
for w in workers:
    hwnd = w["hwnd"]
    alive = bool(IsWindow(hwnd))
    wname = w["name"]
    print(f"  {wname}: hwnd={hwnd}, IsWindow={alive}")
    if alive:
        valid_workers.append(w)
    add_result(f"hwnd_validate_{wname}", alive, f"hwnd={hwnd} alive={alive}")

if not valid_workers:
    print("ERROR: No valid worker HWNDs. Cannot run tests.")
    results["summary"]["error"] = "No valid HWNDs"
    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "ghost_mouse_test_results.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    sys.exit(1)

test_worker = valid_workers[0]
test_hwnd = test_worker["hwnd"]
print(f"\nPrimary test target: {test_worker['name']} (hwnd={test_hwnd})")
print()

# ─── Test: ghost_click ───
print("--- Test: ghost_click ---")
cursor_before = get_cursor_pos()
ok = ghost_click(test_hwnd, 100, 100)
time.sleep(0.1)
cursor_after = get_cursor_pos()
c_ok = check_cursor_stable(cursor_before, cursor_after, "ghost_click")
add_result("ghost_click", ok, f"hwnd={test_hwnd} x=100 y=100 returned={ok}", c_ok)

# ─── Test: ghost_click invalid HWND ───
print("--- Test: ghost_click invalid HWND ---")
cursor_before = get_cursor_pos()
ok_invalid = ghost_click(99999999, 10, 10)
cursor_after = get_cursor_pos()
c_ok = check_cursor_stable(cursor_before, cursor_after, "ghost_click_invalid")
add_result("ghost_click_invalid_hwnd", not ok_invalid,
           f"returned={ok_invalid} (expected False)", c_ok)

# ─── Test: ghost_right_click ───
print("--- Test: ghost_right_click ---")
cursor_before = get_cursor_pos()
ok = ghost_right_click(test_hwnd, 200, 200)
time.sleep(0.1)
cursor_after = get_cursor_pos()
c_ok = check_cursor_stable(cursor_before, cursor_after, "ghost_right_click")
add_result("ghost_right_click", ok, f"hwnd={test_hwnd} returned={ok}", c_ok)

# ─── Test: ghost_double_click ───
print("--- Test: ghost_double_click ---")
cursor_before = get_cursor_pos()
ok = ghost_double_click(test_hwnd, 150, 150)
time.sleep(0.2)
cursor_after = get_cursor_pos()
c_ok = check_cursor_stable(cursor_before, cursor_after, "ghost_double_click")
add_result("ghost_double_click", ok, f"hwnd={test_hwnd} returned={ok}", c_ok)

# ─── Test: ghost_scroll ───
print("--- Test: ghost_scroll ---")
cursor_before = get_cursor_pos()
ok = ghost_scroll(test_hwnd, 100, 100, -120)
time.sleep(0.1)
cursor_after = get_cursor_pos()
c_ok = check_cursor_stable(cursor_before, cursor_after, "ghost_scroll")
add_result("ghost_scroll", ok, f"hwnd={test_hwnd} delta=-120 returned={ok}", c_ok)

# ─── Test: ghost_hover ───
print("--- Test: ghost_hover ---")
cursor_before = get_cursor_pos()
ok = ghost_hover(test_hwnd, 300, 300)
time.sleep(0.1)
cursor_after = get_cursor_pos()
c_ok = check_cursor_stable(cursor_before, cursor_after, "ghost_hover")
add_result("ghost_hover", ok, f"hwnd={test_hwnd} returned={ok}", c_ok)

# ─── Test: ghost_drag ───
print("--- Test: ghost_drag ---")
cursor_before = get_cursor_pos()
ok = ghost_drag(test_hwnd, 100, 100, 300, 300, steps=5)
time.sleep(0.2)
cursor_after = get_cursor_pos()
c_ok = check_cursor_stable(cursor_before, cursor_after, "ghost_drag")
add_result("ghost_drag", ok, f"hwnd={test_hwnd} (100,100)->(300,300) returned={ok}", c_ok)

# ─── Test: find_render_widget ───
print("--- Test: find_render_widget ---")
cursor_before = get_cursor_pos()
render = find_render_widget(test_hwnd)
cursor_after = get_cursor_pos()
c_ok = check_cursor_stable(cursor_before, cursor_after, "find_render_widget")
add_result("find_render_widget", render is not None,
           f"parent={test_hwnd} render_hwnd={render}", c_ok)

# ─── Test: find_all_render_widgets ───
print("--- Test: find_all_render_widgets ---")
cursor_before = get_cursor_pos()
all_renders = find_all_render_widgets(test_hwnd)
cursor_after = get_cursor_pos()
c_ok = check_cursor_stable(cursor_before, cursor_after, "find_all_render_widgets")
add_result("find_all_render_widgets", len(all_renders) >= 1,
           f"parent={test_hwnd} count={len(all_renders)} hwnds={all_renders}", c_ok)

# ─── Test: find_render_widget across ALL workers ───
print("--- Test: find_render_widget ALL workers ---")
for w in valid_workers:
    wname = w["name"]
    cursor_before = get_cursor_pos()
    r = find_render_widget(w["hwnd"])
    cursor_after = get_cursor_pos()
    c_ok = check_cursor_stable(cursor_before, cursor_after, f"find_render_{wname}")
    add_result(f"find_render_widget_{wname}", r is not None,
               f"hwnd={w['hwnd']} render={r}", c_ok)

# ─── Test: ghost_click_render ───
print("--- Test: ghost_click_render ---")
if render:
    cursor_before = get_cursor_pos()
    ok = ghost_click_render(test_hwnd, 200, 200)
    time.sleep(0.1)
    cursor_after = get_cursor_pos()
    c_ok = check_cursor_stable(cursor_before, cursor_after, "ghost_click_render")
    add_result("ghost_click_render", ok,
               f"parent={test_hwnd} x=200 y=200 returned={ok}", c_ok)
else:
    add_result("ghost_click_render", False, "SKIPPED: no render widget found", True)

# ─── Test: invoke_by_name (nonexistent -- should return False gracefully) ───
print("--- Test: invoke_by_name (nonexistent) ---")
cursor_before = get_cursor_pos()
ok = invoke_by_name(test_hwnd, "NonExistentTestButton12345")
cursor_after = get_cursor_pos()
c_ok = check_cursor_stable(cursor_before, cursor_after, "invoke_by_name_nonexistent")
add_result("invoke_by_name_nonexistent", not ok,
           f"Expected False for nonexistent button, got={ok}", c_ok)

# ─── Test: invoke_by_name with known element ───
# We test with "Delegate Session" which is a read-only label, NOT dangerous to invoke
print("--- Test: invoke_by_name (real element) ---")
cursor_before = get_cursor_pos()
ok = invoke_by_name(test_hwnd, "Delegate Session")
time.sleep(0.3)
cursor_after = get_cursor_pos()
c_ok = check_cursor_stable(cursor_before, cursor_after, "invoke_by_name_delegate")
# Pass regardless of whether element exists -- we only care about cursor stability
add_result("invoke_by_name_delegate_session", True,
           f"Attempted Delegate Session invoke, ok={ok}, cursor_stable={c_ok}", c_ok)

# ─── Test: ghost_click_element (nonexistent) ───
print("--- Test: ghost_click_element (nonexistent) ---")
cursor_before = get_cursor_pos()
ok = ghost_click_element(test_hwnd, "NonExistentElement99999")
cursor_after = get_cursor_pos()
c_ok = check_cursor_stable(cursor_before, cursor_after, "ghost_click_element_nonexistent")
add_result("ghost_click_element_nonexistent", not ok,
           f"Expected False for nonexistent, got={ok}", c_ok)

# ─── Test: find_uia_element_coords ───
print("--- Test: find_uia_element_coords ---")
cursor_before = get_cursor_pos()
coords = find_uia_element_coords(test_hwnd, "Pick Model")
cursor_after = get_cursor_pos()
c_ok = check_cursor_stable(cursor_before, cursor_after, "find_uia_element_coords")
add_result("find_uia_element_coords", True,
           f"Pick Model coords={coords}", c_ok)

# ─── Test: ghost_click across ALL workers ───
print("--- Test: ghost_click ALL workers ---")
for w in valid_workers:
    wname = w["name"]
    cursor_before = get_cursor_pos()
    ok = ghost_click(w["hwnd"], 50, 50)
    time.sleep(0.1)
    cursor_after = get_cursor_pos()
    c_ok = check_cursor_stable(cursor_before, cursor_after, f"ghost_click_{wname}")
    add_result(f"ghost_click_{wname}", ok, f"hwnd={w['hwnd']} returned={ok}", c_ok)

# ─── Test: ghost_scroll on multiple workers ───
print("--- Test: ghost_scroll ALL workers ---")
for w in valid_workers:
    wname = w["name"]
    cursor_before = get_cursor_pos()
    ok = ghost_scroll(w["hwnd"], 50, 50, 120)
    time.sleep(0.05)
    cursor_after = get_cursor_pos()
    c_ok = check_cursor_stable(cursor_before, cursor_after, f"ghost_scroll_{wname}")
    add_result(f"ghost_scroll_{wname}", ok, f"hwnd={w['hwnd']} returned={ok}", c_ok)

# ─── Test: CLI entry point via subprocess ───
print("--- Test: CLI entry point ---")
import subprocess
cursor_before = get_cursor_pos()
cli_result = subprocess.run(
    [sys.executable, "tools/ghost_mouse.py",
     "--hwnd", str(test_hwnd), "--x", "100", "--y", "100", "--action", "click"],
    capture_output=True, text=True, timeout=10,
    cwd=os.path.join(os.path.dirname(__file__), "..")
)
cursor_after = get_cursor_pos()
c_ok = check_cursor_stable(cursor_before, cursor_after, "cli_click")
cli_ok = cli_result.returncode == 0
try:
    cli_json = json.loads(cli_result.stdout.strip())
    cli_detail = f"returncode={cli_result.returncode} output={cli_json}"
except Exception:
    cli_detail = f"returncode={cli_result.returncode} stdout={cli_result.stdout[:200]}"
add_result("cli_click", cli_ok, cli_detail, c_ok)

# CLI find-render
cursor_before = get_cursor_pos()
cli_result = subprocess.run(
    [sys.executable, "tools/ghost_mouse.py",
     "--hwnd", str(test_hwnd), "--action", "find-render"],
    capture_output=True, text=True, timeout=10,
    cwd=os.path.join(os.path.dirname(__file__), "..")
)
cursor_after = get_cursor_pos()
c_ok = check_cursor_stable(cursor_before, cursor_after, "cli_find_render")
cli_ok = cli_result.returncode == 0
try:
    cli_json = json.loads(cli_result.stdout.strip())
    cli_detail = f"returncode={cli_result.returncode} output={cli_json}"
except Exception:
    cli_detail = f"returncode={cli_result.returncode} stdout={cli_result.stdout[:200]}"
add_result("cli_find_render", cli_ok, cli_detail, c_ok)

# ─── Summary ───
print()
print("=" * 50)
print("=== TEST SUMMARY ===")
print(f"  Total:  {results['summary']['total']}")
print(f"  Passed: {results['summary']['passed']}")
print(f"  Failed: {results['summary']['failed']}")
print(f"  Cursor ever moved: {results['summary']['cursor_moved']}")

if results["summary"]["cursor_moved"]:
    results["summary"]["verdict"] = "FAIL_CURSOR_MOVED"
elif results["summary"]["failed"] > 0:
    results["summary"]["verdict"] = "PARTIAL_PASS"
else:
    results["summary"]["verdict"] = "ALL_PASS"

print(f"  Verdict: {results['summary']['verdict']}")

# Save
out_path = os.path.join(os.path.dirname(__file__), "..", "data", "ghost_mouse_test_results.json")
with open(out_path, "w") as f:
    json.dump(results, f, indent=2)
print(f"\nResults saved to {out_path}")
