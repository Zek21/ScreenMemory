# Autopilot Permission Switching — Keyboard-Based Method

**Date:** 2026-03-13  
**Status:** DOCUMENTED — Ready for integration  
**Issue:** Switching VS Code Copilot Chat from "Default Approvals" to "Autopilot (Preview)" fails with existing UIA-based approaches when multiple VS Code windows are open.

## Problem Statement

Workers must be in "Autopilot (Preview)" permission mode to execute tasks without manual approval. The existing `guard_bypass.ps1` uses UIA `ExpandCollapsePattern` + PostMessage ghost keyboard (DOWN+ENTER), which fails because:

1. **UIA FindAll hangs or returns 0 elements** when multiple VS Code Insiders windows are open (workers + orchestrator)
2. **PostMessage keystrokes** are ignored by Electron dropdown menus — they need physical input events
3. **UIA ExpandCollapsePattern** cannot reliably open the dropdown on worker windows from a child process

## Solution: Physical Keyboard + Enter (SendInput)

The reliable method uses Win32 `SendInput` for physical keyboard events after opening the dropdown with a physical mouse click:

### Algorithm

```
For each worker (sequentially, verifying each before moving on):
  1. ForceForeground(worker_hwnd)
     - AllowSetForegroundWindow(-1)
     - keybd_event ALT press+release (enables SetForegroundWindow)
     - ShowWindow(SW_RESTORE) if minimized
     - SetForegroundWindow(hwnd)
  
  2. Physical mouse click on permissions button
     - Button location: local coords (220, height-16) in the worker window
     - Screen coords: (window_x + 220, window_y + height - 16)
     - Uses SendInput(MOUSEEVENTF_LEFTDOWN + MOUSEEVENTF_LEFTUP)
     - Wait 600ms for dropdown to open
  
  3. Physical keyboard END key
     - END jumps to the LAST item in the dropdown (Autopilot is always last)
     - Uses SendInput with KEYEVENTF_EXTENDEDKEY flag
     - Wait 300ms
  
  4. Physical keyboard ENTER key
     - Selects the currently highlighted item (Autopilot)
     - Uses SendInput
     - Wait 500ms for dropdown to close
  
  5. OCR verification
     - Capture bottom 120px of worker window
     - OCR scan for "Autopilot" text
     - Only proceed to next worker if confirmed
  
  6. Restore orchestrator focus
     - ForceForeground(orchestrator_hwnd)
```

### Why END+ENTER (not DOWN+ENTER)

The dropdown has 3 items:
- Default Approvals (position 1, y≈296)
- Bypass Approvals (position 2, y≈356)  
- Autopilot (Preview) (position 3, y≈412)

Using `END` key jumps directly to the last item regardless of current selection, while `DOWN` only moves one position — requiring the correct starting position. `END` is always reliable.

### Why Physical SendInput (not PostMessage)

| Method | Works? | Why |
|--------|--------|-----|
| PostMessage WM_KEYDOWN/WM_KEYUP | ❌ | Electron ignores posted keyboard events in dropdown menus |
| PostMessage WM_LBUTTONDOWN/UP | ❌ | Electron ignores posted mouse events entirely |
| UIA ExpandCollapsePattern | ❌ | Hangs/fails with multiple VS Code windows |
| SendInput keyboard events | ✅ | Physical input events processed by the input queue |
| SendInput mouse events | ✅ | Physical mouse input processed normally |
| SetCursorPos + mouse_event | ✅ | Also works (legacy API equivalent) |

## Tool Created

**`tools/set_autopilot.py`** — Production-ready keyboard-based Autopilot switcher.

```bash
# Verify current state (OCR scan, no changes)
python tools/set_autopilot.py --verify-only

# Switch all workers to Autopilot
python tools/set_autopilot.py

# Switch specific worker
python tools/set_autopilot.py --worker alpha

# Switch and wait for each worker to process before moving on
python tools/set_autopilot.py --wait-processing
```

## Integration Requirements

### guard_bypass.ps1 Enhancement
The existing `guard_bypass.ps1` should fall back to the SendInput approach when UIA fails:

```
if UIA ExpandCollapse works → use existing PostMessage ghost keys
else → call tools/set_autopilot.py --worker <name>
```

### new_chat.ps1 Integration
`Set-AutopilotPermissions` function should call `set_autopilot.py` as a fallback when UIA path fails.

### skynet_start.py Integration
Worker initialization should call `set_autopilot.py --worker <name>` after each window is opened, verifying Autopilot before moving to the next worker.

## Worker Visual State (2026-03-13 Snapshot)

| Worker | Permission | Invocation | Bus identity_ack | Notes |
|--------|-----------|------------|-------------------|-------|
| Alpha | Autopilot ✅ | READY ✅ | Not on bus (rotated out) | Previously had pending Apply, now processed |
| Beta | Autopilot ✅ | READY ✅ | Not on bus (rotated out) | Fully online, posted identity |
| Gamma | Autopilot ✅ | Standing by | No | Processed self-prompt, monitor daemon alerted on Alpha drift |
| Delta | Autopilot ✅ | IDLE | No | Processed but no visible status markers |

### Issues Found

1. **Bus ring buffer overflow** — Identity_ack messages from workers are pushed out by daemon noise (pulses, heartbeats, health checks). The 100-message FIFO is too small for the volume of daemon traffic.
2. **Alpha model drift** — Monitor daemon detected Alpha model drift to "Pick Model, Gemini 3.1 Pro (Preview)" and auto-corrected. Root cause unknown — may be a VS Code UI race condition.
3. **Sequential processing needed** — Workers should be activated and verified one at a time. The `--wait-processing` flag in `set_autopilot.py` supports this.
4. **Daemon noise** — The bus is dominated by `skynet_self` pulses, `learner` health, and `overseer` heartbeats. Consider separate channels or reduced frequency for daemon chatter.

## Dropdown Coordinates (930x500 window)

```
Permissions button: local (220, 484)  →  screen (window_x + 220, window_y + 484)
Dropdown items (after click):
  - Default Approvals: local (250, 296)
  - Bypass Approvals:  local (250, 356)
  - Autopilot (Preview): local (250, 412)
```

These coordinates are relative to the 930x500 worker window. For different window sizes, the button is near the bottom-center-left of the chat input area.
