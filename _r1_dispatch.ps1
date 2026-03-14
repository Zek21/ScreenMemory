# Round 1 Skynet Upgrade Dispatch — Same method as proven self-invocation
# Uses clipboard paste + ALT trick + TOPMOST + prefire verification

Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W32 {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
    public static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
    public static readonly IntPtr HWND_NOTOPMOST = new IntPtr(-2);
}
"@

$orchHwnd = 134616
$workers = @(
    @{ Name="alpha"; Hwnd=721342; RenderHwnd=200108 },
    @{ Name="beta";  Hwnd=134608; RenderHwnd=134606 },
    @{ Name="gamma"; Hwnd=984780; RenderHwnd=393850 },
    @{ Name="delta"; Hwnd=265082; RenderHwnd=1182588 }
)

# Round 1 upgrade prompts — each worker gets a specialized Skynet upgrade task
$prompts = @{
    "alpha" = @"
SKYNET ROUND 1 UPGRADE - ALPHA TASK: Bus Communication Hardening

You are Alpha, a Skynet worker. Execute this upgrade task immediately — no questions, no steering.

OBJECTIVE: Harden the Skynet bus communication system to make it fully reliable.

TASKS:
1. Read `Skynet/server.go` and understand the bus ring buffer (100-msg FIFO)
2. Read `tools/skynet_dispatch.py` — understand `ghost_type_to_worker()` and `guarded_publish()`
3. Read `tools/skynet_spam_guard.py` — understand anti-spam dedup
4. UPGRADE: Create `tools/skynet_bus_persist.py` that:
   - Subscribes to bus messages via polling (GET /bus/messages)
   - Persists ALL messages to `data/bus_archive.jsonl` (append-only)
   - Deduplicates by message ID before appending
   - Provides `get_messages(sender=None, type=None, since=None, limit=100)` function to query archive
   - Provides CLI: `python tools/skynet_bus_persist.py --query --sender=alpha --type=result --limit=10`
   - This prevents critical results from being lost when the 100-msg ring buffer overflows
5. Test it works by running the query CLI

When done, POST result to bus:
```
POST http://localhost:8420/bus/publish
{"sender":"alpha","topic":"orchestrator","type":"result","content":"ROUND1_COMPLETE: Bus persistence layer implemented. Archive at data/bus_archive.jsonl. Query CLI working."}
```
"@

    "beta" = @"
SKYNET ROUND 1 UPGRADE - BETA TASK: Worker Self-Registration Protocol

You are Beta, a Skynet worker. Execute this upgrade task immediately — no questions, no steering.

OBJECTIVE: Fix worker registration so backend /status shows real worker states instead of UNREGISTERED.

TASKS:
1. Read `Skynet/server.go` — understand how worker registration and status tracking works
2. Read the `/worker/{name}/heartbeat` endpoint — understand what it expects
3. Read `tools/skynet_monitor.py` — understand how it posts heartbeats
4. UPGRADE: Create `tools/skynet_worker_register.py` that:
   - On startup, each worker calls POST /worker/{name}/register with {hwnd, model, state}
   - Provides a heartbeat function that workers call every 30s: POST /worker/{name}/heartbeat with {state, model}
   - Provides CLI: `python tools/skynet_worker_register.py --register --name=beta --hwnd=134608 --model=claude-opus-4.6-fast`
   - Provides CLI: `python tools/skynet_worker_register.py --heartbeat --name=beta --state=IDLE`
   - Provides `register_all()` that reads `data/workers.json` and registers all workers at once
5. Run `register_all()` to register all 4 workers right now
6. Verify with GET http://localhost:8420/status — workers should no longer show UNREGISTERED

When done, POST result to bus:
```
POST http://localhost:8420/bus/publish
{"sender":"beta","topic":"orchestrator","type":"result","content":"ROUND1_COMPLETE: Worker registration protocol implemented. All 4 workers registered. Status endpoint now shows real states."}
```
"@

    "gamma" = @"
SKYNET ROUND 1 UPGRADE - GAMMA TASK: Skynet Intelligence Dashboard Upgrade

You are Gamma, a Skynet worker. Execute this upgrade task immediately — no questions, no steering.

OBJECTIVE: Upgrade the GOD Console dashboard to show real Skynet intelligence data.

TASKS:
1. Read `god_console.py` — understand the Flask server and endpoints
2. Read `dashboard.html` — understand the current dashboard UI
3. Read `data/worker_scores.json` — understand scoring data
4. Read `data/brain_config.json` — understand brain configuration
5. UPGRADE the dashboard to add these panels:
   a. **Worker Score Leaderboard** — show each worker's score from worker_scores.json, sorted by score
   b. **Bus Activity Feed** — live feed showing last 20 bus messages (poll /bus/messages every 5s)
   c. **TODO Queue Status** — show pending/active/done counts from data/todos.json
   d. **Dispatch Log** — show last 10 dispatches from data/dispatch_log.json with success/fail indicators
   e. **System Health** — show backend uptime, worker count, consultant bridge status
6. The dashboard already exists at http://localhost:8421/dashboard — enhance it, don't replace it
7. Test by opening the dashboard in a browser and verifying real data appears

When done, POST result to bus:
```
POST http://localhost:8420/bus/publish
{"sender":"gamma","topic":"orchestrator","type":"result","content":"ROUND1_COMPLETE: Dashboard upgraded with 5 new intelligence panels: scores, bus feed, TODO status, dispatch log, system health. All showing real data."}
```
"@

    "delta" = @"
SKYNET ROUND 1 UPGRADE - DELTA TASK: Automated Cross-Validation System

You are Delta, a Skynet worker. Execute this upgrade task immediately — no questions, no steering.

OBJECTIVE: Build an automated cross-validation system so workers can verify each other's work.

TASKS:
1. Read `tools/skynet_scoring.py` — understand the scoring system
2. Read `tools/skynet_todos.py` — understand TODO tracking
3. Read `tools/skynet_dispatch.py` — understand dispatch mechanism
4. UPGRADE: Create `tools/skynet_crossval.py` that:
   - When a worker reports DONE on a task, automatically assigns validation to a DIFFERENT worker
   - Validator checks: does the code compile? do tests pass? does the change match the task description?
   - Validator posts result: `{type: "crossval_result", content: {task_id, validator, original_worker, passed: bool, notes: str}}`
   - On pass: +0.01 to original worker, +0.01 to validator
   - On fail: -0.005 to original worker, +0.01 to validator for catching issue
   - Provides CLI: `python tools/skynet_crossval.py --validate --task-id=X --validator=delta`
   - Provides CLI: `python tools/skynet_crossval.py --status` to show pending validations
   - Integrates with `data/worker_scores.json` to update scores automatically
5. Create `data/crossval_queue.json` for tracking pending validations
6. Test by creating a mock validation entry and processing it

When done, POST result to bus:
```
POST http://localhost:8420/bus/publish
{"sender":"delta","topic":"orchestrator","type":"result","content":"ROUND1_COMPLETE: Cross-validation system implemented. Auto-assigns validators, tracks in crossval_queue.json, updates scores. CLI working."}
```
"@
}

$WM_PASTE = 0x0302
$VK_MENU = 0x12
$KEYEVENTF_KEYUP = 0x0002
$SWP_NOMOVE = 0x0002
$SWP_NOSIZE = 0x0001

function Dispatch-ToWorker($name, $hwnd, $renderHwnd, $prompt) {
    Write-Host "[$name] Dispatching to hwnd=$hwnd render=$renderHwnd..."

    # Step 1: Minimize orchestrator to free foreground lock
    [W32]::ShowWindow([IntPtr]$orchHwnd, 6) | Out-Null  # SW_MINIMIZE
    Start-Sleep -Milliseconds 300

    # Step 2: Set target TOPMOST
    [W32]::SetWindowPos([IntPtr]$hwnd, [W32]::HWND_TOPMOST, 0, 0, 0, 0, ($SWP_NOMOVE -bor $SWP_NOSIZE)) | Out-Null

    # Step 3: ALT trick to allow SetForegroundWindow
    [W32]::keybd_event($VK_MENU, 0, 0, [UIntPtr]::Zero)
    [W32]::SetForegroundWindow([IntPtr]$hwnd) | Out-Null
    [W32]::keybd_event($VK_MENU, 0, $KEYEVENTF_KEYUP, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 400

    # Step 4: PREFIRE CHECK — verify foreground is our target
    $fg = [W32]::GetForegroundWindow()
    if ($fg -ne [IntPtr]$hwnd) {
        Write-Host "[$name] PREFIRE FAIL: fg=$fg expected=$hwnd, retrying..."
        Start-Sleep -Milliseconds 500
        [W32]::keybd_event($VK_MENU, 0, 0, [UIntPtr]::Zero)
        [W32]::SetForegroundWindow([IntPtr]$hwnd) | Out-Null
        [W32]::keybd_event($VK_MENU, 0, $KEYEVENTF_KEYUP, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 400
        $fg = [W32]::GetForegroundWindow()
        if ($fg -ne [IntPtr]$hwnd) {
            Write-Host "[$name] PREFIRE FAIL x2: SKIPPING"
            # Remove TOPMOST
            [W32]::SetWindowPos([IntPtr]$hwnd, [W32]::HWND_NOTOPMOST, 0, 0, 0, 0, ($SWP_NOMOVE -bor $SWP_NOSIZE)) | Out-Null
            return "FAILED"
        }
    }
    Write-Host "[$name] PREFIRE OK: foreground confirmed"

    # Step 5: Copy prompt to clipboard and paste
    [System.Windows.Forms.Clipboard]::SetText($prompt)
    Start-Sleep -Milliseconds 200

    # Step 6: Paste to render widget
    [W32]::PostMessage([IntPtr]$renderHwnd, $WM_PASTE, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null
    Start-Sleep -Milliseconds 300

    # Step 7: Send Enter via PostMessage
    [W32]::PostMessage([IntPtr]$renderHwnd, 0x0100, [IntPtr]13, [IntPtr]::Zero) | Out-Null  # WM_KEYDOWN VK_RETURN
    Start-Sleep -Milliseconds 100
    [W32]::PostMessage([IntPtr]$renderHwnd, 0x0101, [IntPtr]13, [IntPtr]::Zero) | Out-Null  # WM_KEYUP VK_RETURN

    # Step 8: Remove TOPMOST
    [W32]::SetWindowPos([IntPtr]$hwnd, [W32]::HWND_NOTOPMOST, 0, 0, 0, 0, ($SWP_NOMOVE -bor $SWP_NOSIZE)) | Out-Null

    Write-Host "[$name] DELIVERED"
    return "DELIVERED"
}

# Dispatch to all 4 workers sequentially
$results = @{}
foreach ($w in $workers) {
    $name = $w.Name
    $prompt = $prompts[$name]
    $result = Dispatch-ToWorker $name $w.Hwnd $w.RenderHwnd $prompt
    $results[$name] = $result
    Start-Sleep -Milliseconds 500
}

# Restore orchestrator
[W32]::ShowWindow([IntPtr]$orchHwnd, 9) | Out-Null  # SW_RESTORE
Start-Sleep -Milliseconds 300
[W32]::SetForegroundWindow([IntPtr]$orchHwnd) | Out-Null

Write-Host ""
Write-Host "=== ROUND 1 DISPATCH SUMMARY ==="
foreach ($k in $results.Keys) {
    Write-Host "  $k : $($results[$k])"
}

$delivered = ($results.Values | Where-Object { $_ -eq "DELIVERED" }).Count
Write-Host "DELIVERED: $delivered / 4"
