## MODEL_GUARD Cross-Validation Caveats Fixed

**Summary:**
pyautogui hardware-level input is now used for VS Code quickpick overlays, with dynamic UIA coordinates for robust model selection. This bypasses Win32/UIA limitations and ensures reliable model selection for Claude Opus 4.6 fast.

**Details:**
- VS Code quickpick overlays are rendered by Chromium and are unreachable by Win32, UIA, or clipboard-based input.
- pyautogui click/typewrite reliably interacts with overlays, enabling robust model selection.
- Dynamic UIA coordinates are used to locate the Pick Model button and input field, adapting to window layout changes.
- This fix resolves previous failures where model guard could not reliably select Opus fast.

**Signed:** alpha
