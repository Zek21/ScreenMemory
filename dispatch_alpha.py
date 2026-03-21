import ctypes, time, pyautogui, pyperclip
u32 = ctypes.windll.user32

ORCH_HWND = 460966
HWND = 264680
gx, gy = 1930, 20

task = """V12 WAVE 1 — CREATE STANDALONE BENCHMARK SCRIPTS FOR PUBLIC GITHUB

You are Alpha, the Primary Builder.

TASK: Create STANDALONE versions of 3 benchmark scripts that can be published on a PUBLIC Github repo. These must have ZERO internal imports — no core/, no tools/, no skynet, no internal modules. They must run independently with only pip-installable dependencies.

IMPORTANT SECURITY: Do NOT include any of these terms or references: Skynet, Copilot CLI, GodMode, god_mode, god_console, ghost_type, winctl, uia_engine, skynet_dispatch, AGENTS.md, bus/publish, localhost:8420, or any internal system names.

Create these 3 files in D:\\Prospects\\ScreenMemory\\data\\github_snippets\\:

1. benchmark_iswindow.py — Standalone version of tools/benchmark_iswindow.py
   - Read the original: tools/benchmark_iswindow.py
   - Must use ONLY: ctypes, time, statistics, argparse, json (all stdlib)
   - Benchmark IsWindow API latency
   - Add nice header comment: "Benchmark: Win32 IsWindow API Latency"
   - Add paper reference: "From: Measured Improvements in Visual Desktop Control (2026)"
   - Remove any internal references

2. benchmark_capture.py — Standalone screen capture benchmark
   - Read the original: tools/benchmark_capture.py
   - Must use ONLY: mss, time, statistics, argparse, json, Pillow (pip-installable)
   - Do NOT import from core.capture or any internal module
   - Implement capture benchmark using mss directly
   - Test multiple region sizes
   - Add paper reference header

3. benchmark_compression.py — Standalone accessibility tree compression benchmark
   - Read the original: tools/benchmark_compression.py  
   - Must use ONLY: json, time, statistics, argparse (all stdlib)
   - Generate synthetic accessibility tree data (do NOT use CDP or any browser connection)
   - Demonstrate the compression algorithm independently
   - NO import from tools.chrome_bridge or any internal module
   - Add paper reference header

Also create:
4. README.md in data/github_snippets/ explaining:
   - What the benchmarks measure
   - How to run them (pip install mss Pillow, then python benchmark_X.py)
   - Expected results
   - Citation info for the paper

5. requirements.txt with: mss, Pillow

DELIVERABLE: Write all 5 files to data/github_snippets/
Post result to bus when done.
"""

old_clip = pyperclip.paste()
pyperclip.copy(task)

u32.SetForegroundWindow(HWND)
time.sleep(1.0)
pyautogui.press('escape')
time.sleep(0.3)
pyautogui.click(gx + 465, gy + 415)
time.sleep(0.5)
pyautogui.hotkey('ctrl', 'a')
time.sleep(0.2)
pyautogui.press('delete')
time.sleep(0.3)
pyautogui.hotkey('ctrl', 'v')
time.sleep(0.5)
pyautogui.press('enter')
time.sleep(0.5)
pyautogui.click(gx + 880, gy + 453)
time.sleep(1.0)
pyperclip.copy(old_clip if old_clip else '')
u32.SetForegroundWindow(ORCH_HWND)
time.sleep(0.5)
print("Alpha Wave 1 dispatched")
