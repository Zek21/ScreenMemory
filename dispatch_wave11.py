import ctypes, time, pyautogui, pyperclip
u32 = ctypes.windll.user32

ORCH_HWND = 460966
HWND = 264680
gx, gy = 1930, 20

task = """WAVE 11 — CROSS-VALIDATION SERIES 2 — VERIFY CODING & BENCHMARK CHANGES

You are Alpha, the Primary Builder. Cross-validate ALL code changes from Waves 4-6.

TASK: Verify every code modification made by other workers in Waves 4-6:

1. DELTA'S SCORING FIXES (Wave 4) — Read data/research_review/wave4_delta_scoring.md then verify:
   - tools/skynet_scoring.py: ZTB_AWARD should be 0.1 (was 1.0)
   - ZTB_COOLDOWN should be 3600 (was 300)
   - MAX_ZTB_PER_SESSION should be 3
   - ORCHESTRATOR_ZTB_RATE should be 0.5
   - VALID_SCORING_AGENTS frozenset should exist
   - Run: python -c "from tools.skynet_scoring import ZTB_AWARD, ZTB_COOLDOWN, MAX_ZTB_PER_SESSION; print(f'ZTB={ZTB_AWARD}, CD={ZTB_COOLDOWN}, MAX={MAX_ZTB_PER_SESSION}')"

2. BETA'S CODE DOC FIXES (Wave 5) — Read data/research_review/wave5_beta_code_fixes.md then verify:
   - core/capture.py: DXGICapture docstring mentions "GDI BitBlt via mss" (NOT DXGI)
   - tools/chrome_bridge/god_mode.py: Token count says ~1400 (NOT 2000)
   - tools/uia_engine.py: Has 192ms provenance note

3. GAMMA'S BENCHMARKS (Wave 5-6) — Read data/research_review/wave5_gamma_benchmarks.md and wave9_gamma_reproducibility.md:
   - Verify scripts exist: tools/benchmark_iswindow.py, tools/benchmark_capture.py, tools/benchmark_compression.py
   - Run each script briefly to confirm they execute without errors
   - Compare Wave 5 vs Wave 9 results for consistency

DELIVERABLE: Write cross-validation report to data/research_review/wave11_alpha_cv_coding.md
Rate each change: VERIFIED/UNVERIFIED/PARTIAL with evidence
Post result summary to bus when done.
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
print("Alpha Wave 11 dispatched")
