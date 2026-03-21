import ctypes, time, pyautogui, pyperclip
u32 = ctypes.windll.user32

ORCH_HWND = 460966
workers = [
    ('Alpha', 264680, 1930, 20),
    ('Beta', 723422, 2870, 20),
    ('Gamma', 1116666, 1930, 540),
    ('Delta', 395780, 2870, 540),
]

tasks = {
    'Alpha': """WAVE 12 — FINAL SIGN-OFF — VERIFY BETA'S DOCX FIXES

You are Alpha, the Primary Builder. This is the FINAL wave.

TASK: Verify that Beta applied the 3 fixes from Wave 8 audit to the docx:

1. Extract text from docx: D:\\Portfolio\\exzilcalanza-blogs\\posts-generated\\screenmemory-ai-desktop-agent-2026-v11.docx
2. Check fix #1: Tables 1-3 should now be referenced in body text BEFORE they appear
   - Look for "Table 1" reference before the Table 1 caption
   - Look for "Table 2" reference before Table 2
   - Look for "Table 3" reference before Table 3
3. Check fix #2: Conclusion should say "approximately 89.5%" NOT "84-90%"
4. Check fix #3: Reference [14] Tesseract should either be cited in body OR removed from references
5. Extract final paper text to data/research_review/v11_full_text_v4_final.txt

DELIVERABLE: Write final sign-off to data/research_review/wave12_alpha_signoff.md
Include: which fixes were applied, which weren't, final quality verdict
Post result to bus when done.""",

    'Beta': """WAVE 12 — FINAL SIGN-OFF — DOCUMENT YOUR WAVE 11 FIXES

You are Beta, the Infrastructure specialist. This is the FINAL wave.

TASK: You modified the docx in Wave 11 but didn't write a summary. Document what you did:

1. Read the current docx: D:\\Portfolio\\exzilcalanza-blogs\\posts-generated\\screenmemory-ai-desktop-agent-2026-v11.docx
2. Check which of your 3 planned fixes from Wave 8 you actually applied:
   - Fix #1: Did you add Table 1-3 inline references?
   - Fix #2: Did you change "84-90%" to "approximately 89.5%" in the conclusion?
   - Fix #3: Did you handle orphaned reference [14] (cite or remove)?
3. If any fix was NOT applied, apply it now
4. Write comprehensive summary

DELIVERABLE: Write to data/research_review/wave12_beta_final_summary.md
List every change with paragraph numbers. Post result to bus when done.""",

    'Gamma': """WAVE 12 — FINAL SIGN-OFF — PUBLICATION READINESS ASSESSMENT

You are Gamma, the Research & Security specialist. This is the FINAL wave.

TASK: Provide a comprehensive publication readiness score for the paper.

1. Read the final paper text from docx: D:\\Portfolio\\exzilcalanza-blogs\\posts-generated\\screenmemory-ai-desktop-agent-2026-v11.docx
2. Score each dimension (1-10):
   - Statistical rigor: Are all claims properly supported with n, CI, p-values?
   - Internal consistency: Do abstract, body, and conclusion agree?
   - Reference integrity: Are all citations correct, verified, and properly formatted?
   - Methodology transparency: Is the cross-validation protocol clearly described?
   - Reproducibility: Could an independent researcher reproduce the results?
   - Writing quality: Is the prose clear, precise, and appropriate for IEEE?
   - Completeness: Are there gaps in the argument or missing data?
3. Identify any remaining issues (if any)
4. Give overall publication readiness verdict: READY / NEEDS REVISION / NOT READY

DELIVERABLE: Write to data/research_review/wave12_gamma_readiness.md
Post result to bus when done.""",

    'Delta': """WAVE 12 — FINAL SIGN-OFF — COMPREHENSIVE CROSS-VALIDATION

You are Delta, the Testing & Validation specialist. This is the FINAL wave.

TASK: Final cross-validation of the ENTIRE 12-wave operation.

1. Read ALL wave output files in data/research_review/ (there should be ~25+ files)
2. Create a comprehensive audit trail:
   - List every issue found across all waves (CRITICAL, MAJOR, MINOR)
   - For each issue: was it FIXED, VERIFIED by cross-validation, or STILL OPEN?
   - Count: total issues found, total fixed, total verified, total remaining
3. Score fairness verification:
   - Review data/worker_scores.json for current scores
   - Were scoring changes (SF-1 to SF-6) applied correctly? (Already verified in Wave 7)
   - Any remaining fairness concerns?
4. Sign off on the paper as publication-ready (or not)

DELIVERABLE: Write to data/research_review/wave12_delta_final_audit.md
Include the complete issue tracker table. Post result to bus when done.""",
}

for name, hwnd, gx, gy in workers:
    task = tasks[name]
    old_clip = pyperclip.paste()
    pyperclip.copy(task)
    
    u32.SetForegroundWindow(hwnd)
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
    time.sleep(2.0)  # 2s cooldown between dispatches
    print(f'{name} Wave 12 dispatched')

print('ALL 4 WORKERS DISPATCHED — Wave 12 Final Consensus')
