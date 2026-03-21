import ctypes, time, pyautogui, pyperclip
u32 = ctypes.windll.user32
ORCH = 460966

# ---- DISPATCH ALPHA ----
HWND = 264680; gx = 1930; gy = 20

task_alpha = (
    'WAVE 8 TESTING SERIES 2 - Extract the FULL updated paper text from the docx for final review. '
    'Read D:\\Portfolio\\exzilcalanza-blogs\\posts-generated\\screenmemory-ai-desktop-agent-2026-v11.docx '
    'using python-docx. Extract ALL paragraph text. Count total paragraphs, tables, sections. '
    'Write the full extracted text to data/research_review/v11_full_text_v3.txt. '
    'Also write a summary of what changed since v2 (compare with data/research_review/v11_full_text_v2.txt if it exists, otherwise v11_full_text.txt). '
    'Write summary to data/research_review/wave8_alpha_extract.md. Post to bus when done.'
)

for attempt in range(3):
    u32.SetForegroundWindow(HWND)
    time.sleep(0.5)
    if u32.GetForegroundWindow() == HWND:
        break
time.sleep(1.5)

pyautogui.press('escape')
time.sleep(0.5)
pyautogui.click(gx + 465, gy + 415)
time.sleep(1.0)
pyautogui.hotkey('ctrl', 'a')
time.sleep(0.3)
pyautogui.press('delete')
time.sleep(0.5)

old_clip = pyperclip.paste()
pyperclip.copy(task_alpha)
time.sleep(0.5)
pyautogui.hotkey('ctrl', 'v')
time.sleep(1.5)
pyautogui.press('enter')
time.sleep(3.0)
pyautogui.click(gx + 880, gy + 453)
time.sleep(1.0)

pyperclip.copy(old_clip if old_clip else '')
u32.SetForegroundWindow(ORCH)
time.sleep(3)

from tools.uia_engine import get_engine
e = get_engine()
s = e.scan(HWND)
print(f'Alpha state after dispatch: {s.state}')

# ---- DISPATCH BETA ----
time.sleep(3)  # clipboard cooldown
HWND = 723422; gx = 2870; gy = 20

task_beta = (
    'WAVE 8 TESTING SERIES 2 - Full paper internal consistency audit. '
    'Read D:\\Portfolio\\exzilcalanza-blogs\\posts-generated\\screenmemory-ai-desktop-agent-2026-v11.docx using python-docx. '
    'Check: (1) All section numbers sequential, no gaps/duplicates (2) All cross-references match section numbers '
    '(3) All table references match tables (4) Abstract claims match body evidence '
    '(5) Conclusion claims match results (6) No orphaned citations. '
    'Write results to data/research_review/wave8_beta_consistency.md. Post to bus when done.'
)

for attempt in range(3):
    u32.SetForegroundWindow(HWND)
    time.sleep(0.5)
    if u32.GetForegroundWindow() == HWND:
        break
time.sleep(1.5)

pyautogui.press('escape')
time.sleep(0.5)
pyautogui.click(gx + 465, gy + 415)
time.sleep(1.0)
pyautogui.hotkey('ctrl', 'a')
time.sleep(0.3)
pyautogui.press('delete')
time.sleep(0.5)

old_clip = pyperclip.paste()
pyperclip.copy(task_beta)
time.sleep(0.5)
pyautogui.hotkey('ctrl', 'v')
time.sleep(1.5)
pyautogui.press('enter')
time.sleep(3.0)
pyautogui.click(gx + 880, gy + 453)
time.sleep(1.0)

pyperclip.copy(old_clip if old_clip else '')
u32.SetForegroundWindow(ORCH)
time.sleep(3)

s = e.scan(HWND)
print(f'Beta state after dispatch: {s.state}')
