import ctypes, time, pyautogui, pyperclip
u32 = ctypes.windll.user32

ORCH_HWND = 460966
HWND = 1116666
gx, gy = 1930, 540

task = """WAVE 9 — TESTING SERIES 3 — FINAL REPRODUCIBILITY VERIFICATION

You are Gamma, the Research & Security specialist. This is your final testing wave.

TASK: Run ALL 3 benchmark scripts that you created in Wave 5 and verify results are STILL consistent:

1. Run: python tools/benchmark_iswindow.py
   - Expected: ~0.33-0.35 microseconds per call
   - Compare with your Wave 5 result: 0.337us

2. Run: python tools/benchmark_capture.py  
   - Expected: ~5ms for 200x200 region
   - Compare with your Wave 5 result: 5.00ms

3. Run: python tools/benchmark_compression.py
   - Expected: ~19-20x compression for synthetic data
   - Compare with your Wave 5 result: similar range

4. Additionally verify the BROKEN cross-reference found in Wave 8 audit:
   - Paragraph P092 references "Section 508" which does NOT exist in the paper
   - Read the v3 text file: data/research_review/v11_full_text_v3.txt
   - Find what P092 actually says and determine what the correct section reference should be
   - Write the fix recommendation

DELIVERABLE: Write results to data/research_review/wave9_gamma_reproducibility.md with:
- All 3 benchmark results with comparison to Wave 5
- Consistency verdict (PASS/FAIL for each)
- The broken cross-reference fix recommendation
- Post result summary to bus when done
"""

old_clip = pyperclip.paste()
pyperclip.copy(task)

u32.SetForegroundWindow(HWND)
time.sleep(1.0)

# Clear any stale content
pyautogui.press('escape')
time.sleep(0.3)
pyautogui.click(gx + 465, gy + 415)
time.sleep(0.5)
pyautogui.hotkey('ctrl', 'a')
time.sleep(0.2)
pyautogui.press('delete')
time.sleep(0.3)

# Paste and submit
pyautogui.hotkey('ctrl', 'v')
time.sleep(0.5)
pyautogui.press('enter')
time.sleep(0.5)
pyautogui.click(gx + 880, gy + 453)
time.sleep(1.0)

# Restore
pyperclip.copy(old_clip if old_clip else '')
u32.SetForegroundWindow(ORCH_HWND)
time.sleep(0.5)
print("Gamma Wave 9 dispatched")
