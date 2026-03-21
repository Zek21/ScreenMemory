import ctypes, time, pyautogui, pyperclip
u32 = ctypes.windll.user32
time.sleep(9)

ORCH_HWND = 460966
HWND = 395780
gx, gy = 2870, 540

task = """V12 WAVE 1 — SCORE FAIRNESS AUDIT + DISPATCH TRACKING

You are Delta, the Testing & Validation specialist.

TASK: Perform a comprehensive score fairness audit and prepare dispatch tracking for the v12 operation.

1. SCORE AUDIT:
   - Read data/worker_scores.json
   - Run: python tools/skynet_scoring.py --leaderboard
   - Check score distribution across all agents
   - Identify any score anomalies or unfair patterns
   - Verify SF-1 through SF-6 fixes are still active in tools/skynet_scoring.py:
     * ZTB_AWARD should be 0.1 (not 1.0)
     * ZTB_COOLDOWN should be 3600
     * ZTB_MAX_PER_SESSION should be 3
     * VALID_SCORING_AGENTS frozenset should exist
   - Check if any agent has a negative score (requires recovery protocol)

2. DISPATCH FAIRNESS BASELINE:
   - Read data/dispatch_log.json (if exists)
   - Count dispatches per worker in the last session
   - Calculate % share: each worker should get ~25%
   - If any worker has <15% or >35%, flag as routing bias

3. V12 OPERATION TRACKING:
   - Create data/research_review/v12_dispatch_tracker.json with structure:
     {
       "operation": "v12_paper_github",
       "waves": {},
       "dispatch_counts": {"alpha": 0, "beta": 0, "gamma": 0, "delta": 0},
       "started": "2026-03-21T12:38:00Z"
     }

DELIVERABLE: Write audit to data/research_review/v12_score_audit.md
Create tracker at data/research_review/v12_dispatch_tracker.json
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
print("Delta Wave 1 dispatched")
