# Skynet Prompt Delivery Pipeline — Definitive Technical Reference

<!-- signed: alpha -->

> **This document is the authoritative reference for understanding how Skynet delivers prompts
> from the orchestrator to workers and consultants.** Every agent MUST read this before proposing
> communication architecture changes. Created per Rule 0.8 (Mandatory Architecture Knowledge)
> following INCIDENT 012.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture Diagram](#2-architecture-diagram)
3. [Entry Points](#3-entry-points)
4. [Ghost Type Mechanism](#4-ghost-type-mechanism)
5. [Pre-Dispatch Visual Check](#5-pre-dispatch-visual-check)
6. [Delivery Verification](#6-delivery-verification)
7. [Clipboard Safety](#7-clipboard-safety)
8. [Consultant Delivery](#8-consultant-delivery)
9. [False Positive Risks](#9-false-positive-risks)
10. [Failure Modes](#10-failure-modes)
11. [Configuration](#11-configuration)
12. [Related Files](#12-related-files)
13. [Incident History](#13-incident-history)

---

## 1. Overview

### What the Delivery Pipeline Does

The Skynet delivery pipeline is the mechanism by which the orchestrator (or any dispatcher)
sends textual prompts into worker and consultant VS Code chat windows. Workers and consultants
are Claude Opus 4.6 (fast mode) sessions running inside VS Code Insiders. They have no API —
the only way to communicate with them is by pasting text into their chat input and pressing Enter.

The pipeline handles:
- **Text preparation**: preamble construction, context enrichment, task wrapping
- **Target resolution**: HWND lookup from `data/workers.json` or consultant state files
- **STEERING cancellation**: auto-clearing draft panels before delivery
- **Clipboard-based paste**: writing text to clipboard, pasting via `SendKeys ^V`, then Enter
- **Focus management**: `AttachThreadInput` for thread-safe focus transfer, orchestrator focus restore
- **Delivery verification**: UIA state transition detection to confirm the worker received the prompt
- **Retry logic**: exponential backoff retries (up to 3 attempts) on unverified delivery
- **Consultant routing**: ghost_type primary with bridge-queue fallback

### Why It Exists

VS Code Copilot CLI sessions have no programmatic input API. The chat input lives inside
Chromium's `Chrome_RenderWidgetHostHWND` render surface, which does not expose standard
Win32 edit controls. The only reliable delivery method is clipboard paste via the OS-level
clipboard (`System.Windows.Forms.Clipboard`) combined with `SendKeys` simulation.

### What INCIDENT 012 Taught Us

Before INCIDENT 012, no agent had ever read the delivery pipeline code. When consultants
needed prompt delivery, agents assumed a hypothetical bridge-queue mechanism would work
without verifying how `ghost_type_to_worker()` actually operates. This led to building
an entire delivery architecture on false assumptions. The fix required:

1. HWND registration in `CC-Start.ps1` and `GC-Start.ps1`
2. Consultant awareness in the consciousness kernel (`skynet_self.py`)
3. Ghost-type delivery to consultants identical to workers
4. Rule 0.8 mandating code-level understanding before architectural proposals

**Lesson: Never assume communication mechanisms — read the code.**

---

## 2. Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                    ORCHESTRATOR (VS Code Session)                    │
│                                                                      │
│  python tools/skynet_dispatch.py --worker alpha --task "..."         │
│                           │                                          │
│                    dispatch_to_worker()                               │
│                     (L1131, skynet_dispatch.py)                       │
└──────────────┬───────────────────────────────────────────────────────┘
               │
               ▼
    ┌─────────────────────┐
    │  ROUTING DECISION   │
    │                     │
    │  worker_name ==     │
    │  "orchestrator"? ──►──── _dispatch_to_orchestrator() (L975)
    │  "consultant" /     │        └── skynet_delivery.deliver_to_orchestrator()
    │  "gemini_consultant"│
    │    ? ──────────────►──── _dispatch_to_consultant() (L1008)
    │  else (worker) ────►──── Continue below ▼
    └─────────────────────┘
               │
               ▼
    ┌─────────────────────────────────────────────────────────┐
    │  PRE-DISPATCH CHECKS                                    │
    │                                                         │
    │  1. Self-dispatch guard (L1137-1141)                     │
    │  2. Load workers.json + orch HWND (L1143-1146)          │
    │  3. IsWindowVisible check (L1161-1163)                  │
    │  4. pre_dispatch_visual_check() (L1165)                 │
    │     └── UIA scan: state, model_ok, agent_ok             │
    │     └── Screenshot capture for debugging                │
    │     └── BLOCK if model_ok=False (security)              │
    │  5. STEERING auto-cancel (L1172-1174)                   │
    │  6. Task enrichment + preamble build (L1179-1180)       │
    │  7. _validate_target_hwnd() (L1182-1184)                │
    └──────────────────────┬──────────────────────────────────┘
                           │
                           ▼
    ┌─────────────────────────────────────────────────────────┐
    │  GHOST TYPE — ghost_type_to_worker() (L948)             │
    │                                                         │
    │  1. Validate target HWND (IsWindow check)               │
    │  2. Write text to temp file (data/.dispatch_tmp_{hwnd})  │
    │     └── Newlines → spaces (single-line safe)            │
    │  3. _build_ghost_type_ps() generates inline PowerShell  │
    │     with C# GhostType class (L704-894)                  │
    │  4. _execute_ghost_dispatch() runs PS subprocess (L897) │
    └──────────────────────┬──────────────────────────────────┘
                           │
                           ▼
    ┌─────────────────────────────────────────────────────────┐
    │  POWERSHELL GHOST TYPE SCRIPT (inline C#)               │
    │  (Generated by _build_ghost_type_ps, L704-894)          │
    │                                                         │
    │  Phase A: STEERING Cancel                               │
    │  ├── Find "Cancel (Alt+Backspace)" button via UIA       │
    │  ├── Invoke via InvokePattern if found                  │
    │  └── Wait 800ms for cleanup                             │
    │                                                         │
    │  Phase B: Input Target Resolution                       │
    │  ├── Enumerate ALL UIA Edit controls in window          │
    │  ├── Score each by: Y-position + left-band + non-Term   │
    │  │   ├── +Y (favor bottom of window)                    │
    │  │   ├── +2000 if X < leftBandMaxX (left 40% of wnd)   │
    │  │   ├── +500 if not "Terminal input"                   │
    │  │   └── +50 if width > 120px                           │
    │  ├── Best-scoring Edit = focusTarget (EDIT method)      │
    │  └── If NO Edit found: FindRender() for                 │
    │      Chrome_RenderWidgetHostHWND (CHROME_RENDER method) │
    │                                                         │
    │  Phase C: Clipboard Verification (3x retry)             │
    │  ├── Save current clipboard to $savedClip               │
    │  ├── SetText($dispatchText)                             │
    │  ├── Read back via GetText() and compare                │
    │  ├── Retry up to 3x on mismatch (50ms/100ms delays)    │
    │  └── Exit 1 with CLIPBOARD_VERIFY_FAILED on failure     │
    │                                                         │
    │  Phase D: Focus + Paste + Enter                         │
    │  ├── EDIT path:                                         │
    │  │   ├── FocusViaAttach(hwnd) — AttachThreadInput       │
    │  │   ├── edit.SetFocus()                                │
    │  │   ├── SendKeys ^V (paste)                            │
    │  │   ├── SendKeys {ENTER}                               │
    │  │   ├── DetachThread(hwnd)                             │
    │  │   └── Status: OK_ATTACHED or OK_FALLBACK             │
    │  └── CHROME_RENDER path:                                │
    │      ├── FocusViaAttach(hwnd)                           │
    │      ├── SetFocus(renderHwnd)                           │
    │      ├── SendKeys ^V (paste)                            │
    │      ├── SendKeys {ENTER}                               │
    │      ├── DetachThread(hwnd)                             │
    │      └── Status: OK_RENDER_ATTACHED or OK_RENDER_FALLB  │
    │                                                         │
    │  Phase E: Clipboard Cleanup                             │
    │  ├── Clipboard.Clear() (30ms after paste)               │
    │  └── Restore $savedClip if it was non-empty             │
    │                                                         │
    │  Phase F: Temp File Cleanup                             │
    │  └── Remove-Item dispatch temp file                     │
    └──────────────────────┬──────────────────────────────────┘
                           │
                           ▼
    ┌─────────────────────────────────────────────────────────┐
    │  DELIVERY VERIFICATION — _verify_delivery() (L1238)     │
    │                                                         │
    │  If pre_state was PROCESSING → auto-verified (queued)   │
    │  Otherwise: poll UIA every 0.5s for up to 8s            │
    │  ├── State changed from pre_state → VERIFIED ✓          │
    │  ├── 3+ consecutive UNKNOWN → FAILED ✗                  │
    │  └── Timeout with no change → UNVERIFIED ⚠              │
    │                                                         │
    │  On UNVERIFIED + pre_state was IDLE:                    │
    │  └── AUTO-RETRY: up to 2 additional attempts            │
    │      with exponential backoff (2s, 4s)                  │
    │      └── Re-check state before retry                    │
    │      └── Re-dispatch ghost_type + verify again          │
    └─────────────────────────────────────────────────────────┘
```

---

## 3. Entry Points

### `dispatch_to_worker()` — Primary Entry Point

**File:** `tools/skynet_dispatch.py`, **Line:** 1131

```python
def dispatch_to_worker(worker_name, task, workers=None, orch_hwnd=None, context=None):
```

**Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `worker_name` | `str` | Target: `"alpha"`, `"beta"`, `"gamma"`, `"delta"`, `"orchestrator"`, `"consultant"`, `"gemini_consultant"` |
| `task` | `str` | The raw task text (before preamble enrichment) |
| `workers` | `list[dict]` | Optional pre-loaded worker list from `workers.json`. Auto-loads if None. |
| `orch_hwnd` | `int` | Optional orchestrator HWND for focus restore. Auto-loads if None. |
| `context` | `dict` | Optional context dict for `build_context_preamble()` instead of default preamble. |

**Return Value:** `bool` — `True` if ghost_type reported delivery success, `False` on any failure.

**Routing Logic (L1148-1152):**
- `"orchestrator"` → `_dispatch_to_orchestrator()` (L975)
- `"consultant"` or `"gemini_consultant"` → `_dispatch_to_consultant()` (L1008)
- Any worker name → full ghost_type pipeline with verification

**Self-Dispatch Guard (L1137-1141):**
```python
self_id = _get_self_identity()
if self_id and self_id.lower() == worker_name.lower():
    log(f"SELF-DISPATCH BLOCKED: {worker_name} tried to dispatch to itself!", "ERR")
    return False
```

### `ghost_type_to_worker()` — Low-Level Delivery

**File:** `tools/skynet_dispatch.py`, **Line:** 948

```python
def ghost_type_to_worker(hwnd, text, orch_hwnd):
```

**Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `hwnd` | `int` | Target window HWND (worker or consultant) |
| `text` | `str` | Full text to deliver (already includes preamble if applicable) |
| `orch_hwnd` | `int` | Orchestrator HWND for focus restore after paste |

**Return Value:** `bool` — `True` if the PowerShell script exited with code 0 and stdout contains `OK_*`.

**Internal Flow:**
1. Validate `hwnd` via `user32.IsWindow(hwnd)` (L958)
2. Write text to `data/.dispatch_tmp_{hwnd}.txt` with newlines replaced by spaces (L964-966)
3. Build PowerShell script via `_build_ghost_type_ps()` (L971)
4. Execute via `_execute_ghost_dispatch()` (L972)

---

## 4. Ghost Type Mechanism

### `_build_ghost_type_ps()` — PowerShell Script Generator

**File:** `tools/skynet_dispatch.py`, **Line:** 704

This function generates an inline PowerShell script containing a C# class (`GhostType`) with
Win32 P/Invoke methods. The script is executed as a single `powershell -NoProfile -Command`
invocation with `CREATE_NO_WINDOW` flag (0x08000000) to avoid console flash.

### C# GhostType Class (L710-744)

The embedded C# class provides these Win32 functions:

| Method | Win32 API | Purpose |
|--------|-----------|---------|
| `SetForegroundWindow(h)` | `user32.dll` | Bring window to front |
| `FindWindowEx(p,c,cls,w)` | `user32.dll` | Enumerate child windows |
| `GetClassName(h,s,n)` | `user32.dll` | Get window class name for render widget detection |
| `GetWindowThreadProcessId(h,pid)` | `user32.dll` | Get thread ID for AttachThreadInput |
| `GetCurrentThreadId()` | `kernel32.dll` | Get current PS thread ID |
| `AttachThreadInput(id1,id2,f)` | `user32.dll` | Attach thread input queues for cross-thread focus |
| `SetFocus(h)` | `user32.dll` | Set keyboard focus |

### `FindRender()` — Recursive Chrome Widget Discovery (L719-728)

```csharp
public static IntPtr FindRender(IntPtr hwnd) {
    var h = FindWindowEx(hwnd, IntPtr.Zero, null, null);
    while (h != IntPtr.Zero) {
        var sb = new StringBuilder(256); GetClassName(h, sb, 256);
        if (sb.ToString().StartsWith("Chrome_RenderWidgetHost")) return h;
        var f = FindRender(h); if (f != IntPtr.Zero) return f;
        h = FindWindowEx(hwnd, h, null, null);
    }
    return IntPtr.Zero;
}
```

This performs a recursive depth-first search through the window tree starting from the
target HWND. It matches any window class starting with `"Chrome_RenderWidgetHost"` (prefix
match for Electron version resilience — the full class name is
`Chrome_RenderWidgetHostHWND`). This is the Chromium render surface inside VS Code where
the chat input lives.

### UIA Edit Scoring Algorithm (L765-791)

When the script cannot find a `Chrome_RenderWidgetHostHWND`, it falls back to UIA Edit
control discovery:

1. Enumerate ALL `ControlType.Edit` elements in the window tree
2. Score each by:
   - **Y-position** (raw `$r.Y`): higher Y = further down = more likely to be chat input
   - **+2000**: if X-coordinate is in the left 40% of window (`$leftBandMaxX = min(340, width*0.4)`)
   - **+500**: if name does NOT match `"Terminal input"` (excludes terminal controls)
   - **+50**: if width > 120px (excludes tiny UI elements)
3. Select the highest-scoring Edit control

### Focus Transfer Mechanism (L836-877)

Two paths exist:

**AttachThreadInput Path (preferred — OK_ATTACHED / OK_RENDER_ATTACHED):**
1. `FocusViaAttach(hwnd)` — attaches PS thread's input queue to target thread
2. `edit.SetFocus()` or `SetFocus(renderHwnd)` — sets keyboard focus
3. `SendKeys ^V` — paste from clipboard
4. `SendKeys {ENTER}` — submit
5. `DetachThread(hwnd)` — clean detach

**SetForegroundWindow Fallback (OK_FALLBACK / OK_RENDER_FALLBACK):**
1. `SetForegroundWindow(hwnd)` — brings target to front (visible focus steal)
2. `edit.SetFocus()` or `SetFocus(renderHwnd)`
3. `SendKeys ^V` + `{ENTER}`
4. `SetForegroundWindow(orchHwnd)` — restore orchestrator focus

### `_execute_ghost_dispatch()` — Subprocess Runner (L897)

**File:** `tools/skynet_dispatch.py`, **Line:** 897

Runs the generated PowerShell under a threading lock (`_dispatch_lock`) to prevent
concurrent clipboard operations. Key behaviors:

- **Lock file**: writes `data/.dispatch_lock.json` during execution (L902-906)
- **Subprocess flags**: `CREATE_NO_WINDOW` (0x08000000), 20-second timeout (L910-913)
- **Success detection (L928-933)**: returncode == 0 AND stdout contains one of:
  `OK_ATTACHED`, `OK_FALLBACK`, `OK_RENDER_ATTACHED`, `OK_RENDER_FALLBACK`
  AND stdout does NOT contain `NO_EDIT` AND stderr is empty
- **CLIPBOARD_VERIFY_FAILED detection (L924-927)**: if stdout contains this marker,
  immediately return False (clipboard race condition detected)

---

## 5. Pre-Dispatch Visual Check

### `pre_dispatch_visual_check()` — UIA + Screenshot Verification

**File:** `tools/skynet_dispatch.py`, **Line:** 495

```python
def pre_dispatch_visual_check(hwnd, worker_name):
    """Returns (ok: bool, state: str, screenshot_path: str|None)."""
```

**What it does:**

1. **UIA Scan** (L504-513): Uses `tools.uia_engine.get_engine().scan(hwnd)` to get:
   - `state`: IDLE / PROCESSING / STEERING / TYPING / UNKNOWN
   - `model_ok`: True if model is Claude Opus 4.6 (fast mode)
   - `agent_ok`: True if agent is Copilot CLI

2. **Screenshot** (L516-525): Uses `Desktop.screenshot(path=..., window=hwnd)` from
   `tools/chrome_bridge/winctl.py` to capture the worker window. Screenshots saved to
   `data/dispatch_screenshots/{worker}_{HHMMSS}.png` with auto-cleanup (keeps last 20).

3. **Security Gate** (L532-534): If `model_ok is False`, the dispatch is **BLOCKED**.
   This prevents prompt delivery to a worker running the wrong model (security violation).

4. **Return**: `(True, state, ss_path)` on success, `(False, state, ss_path)` if model wrong.

### Related UIA Functions

| Function | Line | Purpose |
|----------|------|---------|
| `get_worker_state_uia(hwnd)` | L553 | Returns IDLE/PROCESSING/STEERING/TYPING/UNKNOWN via COM UIA |
| `detect_steering(hwnd)` | L547 | Returns True if STEERING panel visible |
| `wait_for_idle_uia(hwnd, timeout)` | L562 | Polls UIA until IDLE or timeout |
| `confirm_typed_uia(hwnd)` | L578 | Returns True if worker input box has content |

---

## 6. Delivery Verification

### `_verify_delivery()` — State Transition Detection

**File:** `tools/skynet_dispatch.py`, **Line:** 1238

```python
def _verify_delivery(hwnd, worker_name, pre_state, timeout_s=8):
```

**Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `hwnd` | `int` | — | Target worker window HWND |
| `worker_name` | `str` | — | Worker name for logging |
| `pre_state` | `str` | — | Worker state BEFORE dispatch (from visual check) |
| `timeout_s` | `int` | 8 | Maximum seconds to wait for state transition |

**Return Value:** `bool` — `True` if delivery verified, `False` if unverified.

**Algorithm:**

1. **PROCESSING bypass** (L1249): If `pre_state == "PROCESSING"`, return True immediately.
   The worker was already processing — the new dispatch queued in VS Code.

2. **UIA polling loop** (L1256): Poll `engine.get_state(hwnd)` every 0.5 seconds for
   up to `timeout_s * 2` iterations (default: 16 polls over 8 seconds).

3. **State transition detection** (L1267): If `post_state != pre_state` AND
   `post_state != "UNKNOWN"`, delivery is VERIFIED.

4. **UNKNOWN hardening** (L1260-1274): Track consecutive UNKNOWN readings. If 3 or more
   consecutive polls return UNKNOWN (or throw exceptions), treat as FAILED. This prevents
   broken UIA from reporting false positives.

5. **Timeout** (L1275): If no state change detected within timeout, return False (UNVERIFIED).

6. **Import failure** (L1276-1278): If UIA engine cannot be imported, return False
   (cannot verify = FAILED, not assumed success).

### Auto-Retry Logic (L1195-1233)

When `_verify_delivery()` returns False and pre_state was IDLE:

```
Attempt 1: Initial dispatch (already happened)
    └── _verify_delivery() → False (UNVERIFIED)

Attempt 2: Wait 2.0s → re-check state → re-dispatch ghost_type → verify again
    └── If state changed before retry → confirmed, stop

Attempt 3: Wait 4.0s → re-check state → re-dispatch ghost_type → verify again
    └── If still unverified → log warning, track failure
```

**Backoff schedule:** `BACKOFF_BASE * (2 ** (attempt - 2))` = 2s, 4s

**Failure tracking:** `_track_dispatch_failure(worker_name)` logs to
`data/dispatch_failures.json`. `_reset_dispatch_failures(worker_name)` clears on success.

---

## 7. Clipboard Safety

The clipboard is the transport layer for ghost-type delivery. Multiple safety mechanisms
prevent data loss and race conditions:

### Clipboard Save/Restore (L808-885)

1. **Save** (L810): `$savedClip = Clipboard.GetText()` — preserves user clipboard
2. **Restore** (L882-885): After paste, if `$savedClip` was non-empty, restore it

### Clipboard Verification (L811-834)

**Problem:** Between `SetText()` and the actual paste (`SendKeys ^V`), another process
(or Windows itself) can overwrite the clipboard, causing the wrong text to be pasted.

**Solution:** Triple-retry verification loop:

```
for attempt in 1..3:
    Clipboard.SetText($dispatchText)
    sleep 50ms
    $readBack = Clipboard.GetText()
    if $readBack == $dispatchText → verified, proceed
    else → retry with 100ms backoff
```

If all 3 attempts fail, the script outputs `CLIPBOARD_VERIFY_FAILED` and exits with code 1.
`_execute_ghost_dispatch()` detects this marker (L924-927) and returns False.

### Post-Paste Clipboard Clear (L879-881)

After the paste completes:
1. Wait 30ms (let paste complete)
2. `Clipboard.Clear()` — remove sensitive dispatch text from clipboard
3. Then restore saved clipboard (if any)

This prevents stale dispatch data from lingering in the system clipboard where other
processes could read it.

### Dispatch Lock (L900-907)

A Python threading lock (`_dispatch_lock`) ensures only one ghost-type operation executes
at a time. This prevents concurrent clipboard writes from corrupting each other.
A lock file (`data/.dispatch_lock.json`) provides visibility into who holds the lock.

---

## 8. Consultant Delivery

### `_dispatch_to_consultant()` — Dual-Path Delivery

**File:** `tools/skynet_dispatch.py`, **Line:** 1008

Consultants are VS Code windows identical to workers. They receive prompts via the same
ghost_type mechanism. Bridge-queue is a fallback, not the primary path.

**Phase 1: Ghost-Type Primary (L1017-1031)**

```python
consultant_hwnd = load_consultant_hwnd(target_name)
if consultant_hwnd and user32.IsWindow(consultant_hwnd):
    ok = ghost_type_to_worker(consultant_hwnd, task, orch_hwnd or consultant_hwnd)
    if ok:
        # Also post to bridge as audit trail (best-effort)
        deliver_to_consultant(target_name, task, ...)
        return True
```

If the consultant's HWND is registered and the window is alive, ghost_type is used.
On success, the prompt is also posted to the bridge queue as an audit trail (best-effort,
failures are silently ignored).

**Phase 2: Bridge-Queue Fallback (L1035-1050)**

If ghost_type fails (HWND dead, window not found, clipboard failure), falls back to
`skynet_delivery.deliver_to_consultant()` which posts to the consultant's bridge HTTP
endpoint. The bridge daemon queues the prompt for the consultant's consumer to pick up.

### `load_consultant_hwnd()` — HWND Resolution

**File:** `tools/skynet_dispatch.py`, **Line:** 992

Reads consultant HWNDs from state files:
- `"consultant"` → `data/consultant_state.json`
- `"gemini_consultant"` → `data/gemini_consultant_state.json`

These state files are written by `CC-Start.ps1` and `GC-Start.ps1` at boot time via
`GetForegroundWindow()`. They contain:
```json
{
    "hwnd": 12345678,
    "requires_hwnd": true,
    "prompt_transport": "ghost_type",
    "model": "GPT-5 Codex",
    "sender_id": "consultant"
}
```

### Consultant Bridge Architecture

```
┌─────────────────┐     ┌───────────────────┐     ┌──────────────────┐
│ Orchestrator     │────►│ Consultant Bridge │────►│ Consumer Daemon  │
│ dispatch_to_     │     │ HTTP Server       │     │ (polls /next)    │
│ consultant()     │     │ :8422 or :8425    │     │                  │
│                  │     │                   │     │ Relays to bus +  │
│ ghost_type       │     │ Queue: /prompts   │     │ marks complete   │
│ (primary) ──────►│VS   │ Health: /health   │     │                  │
│                  │Code │ ACK: /prompts/ack │     │                  │
│ bridge-queue     │     │                   │     │                  │
│ (fallback) ─────►│     │                   │     │                  │
└─────────────────┘     └───────────────────┘     └──────────────────┘
```

| Consultant | Bridge Port | State File | Consumer Daemon |
|------------|------------|------------|-----------------|
| Codex | 8422 (fallback: 8424) | `data/consultant_state.json` | `tools/skynet_consultant_consumer.py` |
| Gemini | 8425 | `data/gemini_consultant_state.json` | `tools/skynet_consultant_consumer.py` |

---

## 9. False Positive Risks

Nine false-positive risks were identified during a deep pipeline analysis. Each is
documented with its current mitigation status.

### Risk 1: Clipboard Race Condition
**What:** Between `SetText()` and `SendKeys ^V`, another process overwrites the clipboard.
**Impact:** Wrong text pasted into worker — worker receives corrupted/unrelated prompt.
**Mitigation:** ✅ FIXED — Triple-retry clipboard verification with read-back confirmation (L811-834). `CLIPBOARD_VERIFY_FAILED` exits the script on all-3-fail.

### Risk 2: Focus Race Condition
**What:** Between `AttachThreadInput` + `SetFocus` and `SendKeys ^V`, the user clicks
another window, stealing focus. The paste goes to the wrong window.
**Impact:** Prompt delivered to wrong target (potentially dangerous).
**Mitigation:** ⚠️ PARTIAL — `AttachThreadInput` provides thread-level focus which is
more robust than `SetForegroundWindow`, but not immune to user interaction during the
~200ms delivery window. The dispatch lock prevents concurrent deliveries.

### Risk 3: UIA Edit Scoring Wrong Target
**What:** The Edit scoring algorithm selects the wrong Edit control (e.g., a file search
box instead of the chat input). Text is pasted into a non-chat input.
**Impact:** Prompt lost — never reaches the AI.
**Mitigation:** ⚠️ PARTIAL — Scoring heuristic (Y-pos + left-band + non-Terminal + width)
is generally accurate but can fail in unusual VS Code layouts. The Chrome_RenderWidgetHostHWND
fallback (FindRender) is more reliable since it targets the Chromium surface directly.

### Risk 4: Chrome Render Ambiguity in Multi-Pane
**What:** In a split VS Code window with multiple chat panes, `FindRender()` returns
the first `Chrome_RenderWidgetHostHWND` found via DFS, which may not be the intended pane.
**Impact:** Prompt delivered to wrong pane.
**Mitigation:** ⚠️ UNMITIGATED — FindRender uses DFS and returns the first match. No
pane-level targeting exists. The Pre-Fire Visual Proof Rule (Rule 0.015) requires
screenshot verification before typing into shared windows, but this is a protocol rule,
not a code enforcement.

### Risk 5: STEERING Cancel Silent Failure
**What:** The "Cancel (Alt+Backspace)" button is found and `InvokePattern.Invoke()` is
called, but the cancel doesn't actually take effect (VS Code ignores it).
**Impact:** Dispatch text is pasted on top of an active STEERING panel. Worker may
execute the wrong action or the text may be consumed by the steering interface.
**Mitigation:** ⚠️ PARTIAL — 800ms wait after cancel. No post-cancel state verification.
The main `dispatch_to_worker()` does detect STEERING pre-dispatch and calls
`clear_steering_and_send()` separately, but the inline PS cancel has no verification.

### Risk 6: Verify Is Informational Only
**What:** `_verify_delivery()` returns a boolean but `dispatch_to_worker()` does NOT
use it to change the return value. Even if verification fails, the function returns
the `ok` value from `ghost_type_to_worker()`.
**Impact:** Callers believe dispatch succeeded when delivery may have failed silently.
**Mitigation:** ⚠️ BY DESIGN — Verification is intentionally informational. The auto-retry
logic (L1195-1233) handles unverified deliveries by retrying up to 3 times. The outer
return value reflects "we pasted successfully" not "the worker acknowledged receipt."

### Risk 7: PROCESSING Bypass
**What:** When pre_state is PROCESSING, `_verify_delivery()` returns True immediately
without any check. If the worker's PROCESSING state is stale (UIA lag, window not
responding), the delivery may fail but is reported as verified.
**Impact:** False positive verification on a stuck or dead worker.
**Mitigation:** ⚠️ PARTIAL — `skynet_monitor.py` detects PROCESSING > 180s and
auto-cancels via UIA. But between dispatch and monitor detection (up to 3 minutes),
a stuck-PROCESSING worker will silently accept and lose dispatches.

### Risk 8: Stderr = Failure
**What:** Any stderr output from the PowerShell script causes the dispatch to be marked
as failed, even if the paste actually succeeded. PowerShell warnings and .NET assembly
loading messages can appear on stderr.
**Impact:** False negative — successful delivery reported as failure.
**Mitigation:** ⚠️ KNOWN RISK — The strict `and not stderr` check (L932) can produce
false negatives. However, this is a conservative choice — false negatives (retry) are
safer than false positives (assume success).

### Risk 9: UNKNOWN State Ambiguity
**What:** UIA `get_state()` returns "UNKNOWN" when the COM call throws or times out.
Previously, UNKNOWN was treated ambiguously — sometimes as success, sometimes as failure.
**Impact:** Inconsistent verification results. Prior to fix, the outer except in
`_verify_delivery()` returned `True` (assume success on UIA failure).
**Mitigation:** ✅ FIXED — Three consecutive UNKNOWN readings = FAILED (L1260-1274).
Outer except returns `False` instead of `True` (L1278). UNKNOWN excluded from confirmed
state transitions (L1267).

---

## 10. Failure Modes

### Clipboard Locked
**Scenario:** Another application holds an exclusive lock on the clipboard.
**Symptoms:** `SetText()` throws, clipboard verification fails after 3 attempts.
**Output:** `CLIPBOARD_VERIFY_FAILED`
**Recovery:** `_execute_ghost_dispatch()` returns False. Auto-retry kicks in (2s backoff).
**Prevention:** Dispatch lock ensures sequential clipboard access within Skynet.

### Chrome Widget Not Found
**Scenario:** `FindRender()` DFS returns `IntPtr.Zero` — no `Chrome_RenderWidgetHostHWND`
child window exists. AND no UIA Edit control was found.
**Symptoms:** Script outputs `NO_EDIT_NO_RENDER`.
**Output:** Exit code 1.
**Recovery:** dispatch returns False. Caller decides whether to retry.
**Common Cause:** Worker window is closing, VS Code is updating, or the chat panel is not open.

### HWND Dead
**Scenario:** Worker window was closed or VS Code restarted between HWND lookup and dispatch.
**Detection:** `user32.IsWindow(hwnd)` returns False (L958).
**Output:** `ghost_type: invalid target HWND={hwnd}` log entry.
**Recovery:** Ghost type returns False immediately. Worker marked as DEAD by monitor daemon.
**Common Cause:** VS Code crash, manual window close, session restore failure.

### Focus Stolen During Delivery
**Scenario:** User clicks another window during the ~200ms paste window.
**Symptoms:** Text pasted into wrong window, or paste fails silently.
**Impact:** Prompt lost or delivered to wrong target.
**Recovery:** Delivery verification will likely fail (worker state doesn't change).
Auto-retry will re-attempt. The dispatched text is in the temp file until cleanup.
**Prevention:** `AttachThreadInput` provides better focus isolation than `SetForegroundWindow`.

### STEERING Not Cancelled
**Scenario:** "Cancel (Alt+Backspace)" button found but `Invoke()` has no effect.
**Symptoms:** Prompt text enters the steering interface instead of the chat input.
**Detection:** Post-dispatch UIA scan may show STEERING instead of PROCESSING.
**Recovery:** `dispatch_to_worker()` calls `clear_steering_and_send()` as steer-bypass (L1188-1189).

### UIA Engine Import Failure
**Scenario:** `from tools.uia_engine import get_engine` raises an ImportError or the COM
engine fails to initialize.
**Impact:** Pre-dispatch visual check returns UNKNOWN state. Delivery verification returns
False (L1278 — cannot verify = failed).
**Recovery:** Dispatch proceeds without verification. Monitor daemon provides backup monitoring.

### PowerShell Timeout
**Scenario:** The ghost-type PS script exceeds the 20-second subprocess timeout.
**Detection:** `subprocess.run()` raises `TimeoutExpired`.
**Output:** `_execute_ghost_dispatch()` catches the exception and returns False.
**Common Cause:** VS Code hung, extremely large dispatch text, system under heavy load.

---

## 11. Configuration

### `data/brain_config.json` — Dispatch Rules

| Key | Value | Description |
|-----|-------|-------------|
| `dispatch_rules.post_dispatch_uia_verify` | `true` | Enable UIA verification after dispatch |
| `dispatch_rules.never_wait_all` | `true` | Orchestrator never blocks on all workers |
| `dispatch_rules.continuous_dispatch_mode` | `true` | Fire-and-forget dispatch mode |
| `dispatch_rules.auto_click_apply` | `true` | Auto-click Apply dialogs on idle workers |
| `dispatch_rules.scoring_cross_validation_only` | `true` | Points only awarded after cross-validation |
| `dispatch_rules.delivery_mechanism` | `"ghost_type_chrome_render_widget"` | Primary delivery method identifier |
| `dispatch_rules.consultant_transport` | `"ghost_type_primary_bridge_fallback"` | Consultant delivery strategy |
| `dispatch_rules.sequential_verification.enabled` | `true` | Verify sequential dispatches |
| `dispatch_rules.sequential_verification.poll_interval_s` | `3` | UIA poll interval for sequential ops |
| `dispatch_rules.sequential_verification.max_wait_s` | `120` | Max wait for sequential verification |
| `dispatch_rules.sequential_verification.screenshot_verify` | `true` | Take screenshots during sequential dispatch |
| `architecture_knowledge.bus_ring_buffer_size` | `100` | Bus ring buffer capacity (FIFO, ephemeral) |
| `architecture_knowledge.bus_persistence` | `"skynet_bus_persist.py"` | Archival tool for bus messages |
| `architecture_knowledge.consciousness_includes_consultants` | `true` | skynet_self.py knows about consultants |

### Hardcoded Constants

| Constant | Value | Location | Purpose |
|----------|-------|----------|---------|
| PS timeout | 20s | L912 | Maximum subprocess execution time |
| Verify timeout | 8s | L1238 | Default delivery verification window |
| Verify poll interval | 0.5s | L1257 | UIA state check frequency |
| Clipboard verify retries | 3 | L814 | Clipboard SetText verification attempts |
| Clipboard verify delay | 50ms/100ms | L816,824 | Delays between clipboard checks |
| Retry backoff base | 2.0s | L1200 | Exponential backoff base for retries |
| Max retries | 3 total | L1199-1201 | Including initial attempt |
| STEERING cancel wait | 800ms | L760 | Pause after STEERING cancel |
| AttachThread paste delay | 80ms | L840-842 | Delay between paste and enter |
| Chrome render paste delay | 120ms | L861 | Longer delay for Chrome render path |
| Clipboard post-clear delay | 30ms | L880 | Wait before clearing clipboard |
| Dispatch screenshots kept | 20 | L539 | Per-worker screenshot retention |
| Consecutive UNKNOWN threshold | 3 | L1262 | UNKNOWN readings before FAILED |

---

## 12. Related Files

### Core Dispatch Pipeline

| File | Key Functions | Purpose |
|------|---------------|---------|
| `tools/skynet_dispatch.py` | `dispatch_to_worker()` L1131, `ghost_type_to_worker()` L948, `_build_ghost_type_ps()` L704, `_execute_ghost_dispatch()` L897, `_verify_delivery()` L1238, `_dispatch_to_consultant()` L1008, `load_consultant_hwnd()` L992, `pre_dispatch_visual_check()` L495, `build_preamble()` L236, `clear_steering_and_send()` L636, `mark_dispatch_received()` L190, `load_workers()` L681, `load_orch_hwnd()` L693 | Primary dispatch pipeline — ALL prompt delivery flows through this file |

### Unified Delivery System

| File | Key Functions | Purpose |
|------|---------------|---------|
| `tools/skynet_delivery.py` | `deliver()` L923, `deliver_to_orchestrator()` L963, `deliver_to_consultant()` L977, `_deliver_to_consultant_bridge()` L843, `_deliver_to_consultant_ghost_type()` L814, `_ghost_type()` L633, `validate_hwnd()` L480, `resolve_orchestrator_hwnd()` L593 | Abstraction layer over delivery methods — used by dispatch for orchestrator/consultant routing |

### UIA Engine

| File | Key Functions | Purpose |
|------|---------------|---------|
| `tools/uia_engine.py` | `get_engine()`, `engine.scan(hwnd)`, `engine.get_state(hwnd)`, `engine.scan_all(hwnds)`, `engine.cancel_generation(hwnd)`, `engine.wait_for_idle(hwnd)` | COM-based UIA scanner — 7x faster than PowerShell. Provides state detection, model/agent verification, and generation cancellation. |

### Desktop Automation

| File | Key Functions | Purpose |
|------|---------------|---------|
| `tools/chrome_bridge/winctl.py` | `Desktop.screenshot()`, `Desktop.focus()`, `Desktop.windows()` | Win32 window management and screenshot capture for visual checks |

### State Files

| File | Content | Written By |
|------|---------|------------|
| `data/workers.json` | Worker HWNDs, names, positions | `tools/skynet_start.py` |
| `data/orchestrator.json` | Orchestrator HWND | Boot protocol / `Orch-Start.ps1` |
| `data/consultant_state.json` | Codex consultant HWND, transport | `CC-Start.ps1` |
| `data/gemini_consultant_state.json` | Gemini consultant HWND, transport | `GC-Start.ps1` |
| `data/dispatch_log.json` | Dispatch history with timestamps | `_log_dispatch()` |
| `data/dispatch_failures.json` | Consecutive failure tracking | `_track_dispatch_failure()` |
| `data/.dispatch_lock.json` | Active dispatch lock info | `_execute_ghost_dispatch()` |
| `data/.dispatch_tmp_{hwnd}.txt` | Temp dispatch text file | `ghost_type_to_worker()` |
| `data/dispatch_screenshots/` | Pre-dispatch screenshots | `pre_dispatch_visual_check()` |

### Monitoring & Health

| File | Key Functions | Purpose |
|------|---------------|---------|
| `tools/skynet_monitor.py` | Model drift detection, stuck worker auto-cancel | Background daemon — detects PROCESSING > 180s, auto-cancels via UIA |
| `tools/skynet_self.py` | `get_consultant_status()`, `quick_pulse()`, `_check_consultants()` | Consciousness kernel — consultant awareness, HWND liveness checks |
| `tools/skynet_consultant_consumer.py` | `poll_and_relay()` | Daemon — polls bridge queue, ACKs, relays to bus |

### Boot Scripts

| File | Purpose |
|------|---------|
| `CC-Start.ps1` | Codex Consultant bootstrap — HWND detection via GetForegroundWindow, state file write |
| `GC-Start.ps1` | Gemini Consultant bootstrap — same as CC-Start.ps1 for Gemini |
| `Orch-Start.ps1` | Orchestrator bootstrap — infrastructure + worker management |
| `tools/skynet_start.py` | Full system start — backend, workers, daemons |

---

## 13. Incident History

### INCIDENT 009 — Dispatch Queue Corruption

**Date:** 2026-03-11
**What happened:** Multiple ghost-type dispatches fired simultaneously without the dispatch
lock. Concurrent clipboard writes corrupted each other — workers received fragments of
prompts intended for different targets.
**Root cause:** The threading lock (`_dispatch_lock`) was not applied to the clipboard
write-paste-clear cycle. Two dispatch threads could set the clipboard with different text
between each other's SetText and SendKeys calls.
**Fix:** All ghost-type operations now execute under `_dispatch_lock`. The lock file
(`data/.dispatch_lock.json`) provides debugging visibility.
**Lesson:** The clipboard is a shared global resource. Concurrent access without locking
guarantees corruption.

### INCIDENT 010 — False Positive Delivery Verification

**Date:** 2026-03-11
**What happened:** `_verify_delivery()` returned True when UIA calls threw exceptions,
because the outer except clause used `return True` (assume success when verification fails).
Workers appeared to receive prompts they never actually got.
**Root cause:** The original assumption was "if we can't verify, it probably worked."
This is wrong — if UIA is broken, we have no evidence of delivery. The correct default
is "cannot verify = failed."
**Fix:** Outer except changed to `return False`. UNKNOWN state handling hardened: 3+
consecutive UNKNOWN readings = FAILED. UNKNOWN excluded from confirmed state transitions.
**Lesson:** Never assume success when you can't prove it. Silence is not confirmation.

### INCIDENT 012 — Consultant Delivery Architecture Built on False Assumptions

**Date:** 2026-03-12
**What happened:** When consultants needed prompt delivery, agents designed a bridge-queue
architecture without reading `ghost_type_to_worker()` code. They assumed consultants
needed a special mechanism different from workers. In reality, consultants are VS Code
windows identical to workers — they need the exact same ghost_type clipboard-paste delivery.
**Root cause:** No agent had ever read the dispatch pipeline code. Architectural decisions
were made based on assumptions, not code analysis. The bridge-queue mechanism was built
and deployed before anyone discovered the fundamental mismatch.
**Fix:**
1. HWND registration added to `CC-Start.ps1` and `GC-Start.ps1` (GetForegroundWindow)
2. State files include `prompt_transport=ghost_type` and `requires_hwnd=true`
3. `_dispatch_to_consultant()` rewritten: ghost_type primary, bridge fallback
4. Consciousness kernel (`skynet_self.py`) updated with consultant awareness
5. Rule 0.8 (Mandatory Architecture Knowledge) added to AGENTS.md
**Lesson:** NEVER propose architectural changes without reading the relevant code first.
Code is truth. Assumptions are lies waiting to be discovered.

---

## Document Metadata

- **Created:** 2026-03-12T05:14Z
- **Author:** Worker Alpha (cross-validated from source code analysis)
- **Source files verified:** `tools/skynet_dispatch.py` (~1950 lines), `tools/skynet_delivery.py` (~1320 lines), `data/brain_config.json`
- **Line numbers verified against:** Current HEAD as of creation date
- **Rule reference:** Rule 0.8 (Mandatory Architecture Knowledge), AGENTS.md

<!-- signed: alpha -->
