import ctypes, time, pyautogui, pyperclip
u32 = ctypes.windll.user32

ORCH_HWND = 460966
HWND = 395780
gx, gy = 2870, 540

task = """WAVE 10 — CROSS-VALIDATION SERIES 1 — REVIEW ALL CONVENTION CORRECTIONS

You are Delta, the Testing & Validation specialist. Cross-validate ALL convention corrections from Waves 1-3.

TASK: Review every correction made to the docx by Alpha and Beta, checking against original findings:

1. Read the Wave 1 findings:
   - data/research_review/wave1_alpha_convention.md (7 issues, 1 CRITICAL)
   - data/research_review/wave1_beta_convention.md (5 CRITICAL, 4 MAJOR)
   - data/research_review/wave1_gamma_convention.md (4 CRITICAL, 9 MAJOR)
   - data/research_review/wave1_delta_convention.md (7+ issues)

2. Read what corrections were applied:
   - data/research_review/wave2_alpha_changes.md (47 changes applied)
   - data/research_review/wave2_beta_changes.md (14 reference corrections)
   - data/research_review/wave2_gamma_corrections.md (23 stat corrections)
   - data/research_review/wave2_delta_additions.md (related work additions)
   - data/research_review/wave3_beta_crosscheck.md (citation final check)

3. Read the current paper text (v3):
   - data/research_review/v11_full_text_v3.txt

4. Verify:
   - Were ALL CRITICAL issues from Wave 1 addressed?
   - Were the fabricated references actually fixed? (Compare current refs vs Wave 1 findings)
   - Are section numbers sequential and correct (1, 2, 3, 3.1, 3.2, 4, 4.1...)?
   - Is the broken "Section 508" reference in P092 still present?
   - Any remaining issues not caught?

DELIVERABLE: Write cross-validation report to data/research_review/wave10_delta_cv_conventions.md
Rate each correction: VERIFIED/UNVERIFIED/PARTIAL
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
print("Delta Wave 10 dispatched")
