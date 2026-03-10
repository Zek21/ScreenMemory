# new_chat.ps1 - Opens a new detached Copilot CLI chat window via invisible automation
# Uses ghost mouse (PostMessage) and UIA patterns — never moves the user's cursor.
# Enforces: no new window if an existing chat window has no first prompt yet.
# Enforces: new windows are tiled, never overlapping existing chat windows.
# Usage: powershell -ExecutionPolicy Bypass -File tools\new_chat.ps1

param(
    [int]$Monitor = 2,  # 1=left, 2=right
    [int]$Width = 800,
    [int]$Height = 880
)

# --- Session open failure tracker (max 2 consecutive attempts) ---
$failFile = "D:\Prospects\ScreenMemory\data\chat_open_failures.json"
$MAX_CONSECUTIVE_FAILS = 2

if (Test-Path $failFile) {
    try {
        $failData = Get-Content $failFile -Raw | ConvertFrom-Json
        if ($failData.consecutive_failures -ge $MAX_CONSECUTIVE_FAILS) {
            Write-Host "BLOCKED: $MAX_CONSECUTIVE_FAILS consecutive session open failures (last: $($failData.last_failure)). Delete $failFile to retry."
            exit 1
        }
    } catch {}
}

function Record-Failure([string]$reason) {
    $data = @{ consecutive_failures = 1; last_failure = $reason; timestamp = (Get-Date -Format o) }
    if (Test-Path $failFile) {
        try {
            $existing = Get-Content $failFile -Raw | ConvertFrom-Json
            $data.consecutive_failures = $existing.consecutive_failures + 1
        } catch {}
    }
    $data | ConvertTo-Json | Set-Content $failFile
}

function Clear-Failures {
    if (Test-Path $failFile) { Remove-Item $failFile -Force }
}

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public class Ghost {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr hWnd, int X, int Y, int nWidth, int nHeight, bool bRepaint);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] public static extern IntPtr FindWindowEx(IntPtr hwndParent, IntPtr hwndChildAfter, string lpszClass, string lpszWindow);
    [DllImport("user32.dll")] public static extern bool ScreenToClient(IntPtr hWnd, ref POINT lpPoint);
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT lpPoint);
    public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
    [StructLayout(LayoutKind.Sequential)]
    public struct POINT { public int X; public int Y; }

    public static void Click(IntPtr renderHwnd, int screenX, int screenY) {
        POINT pt; pt.X = screenX; pt.Y = screenY;
        ScreenToClient(renderHwnd, ref pt);
        IntPtr lParam = (IntPtr)((pt.Y << 16) | (pt.X & 0xFFFF));
        PostMessage(renderHwnd, 0x0201, (IntPtr)0x0001, lParam);
        System.Threading.Thread.Sleep(50);
        PostMessage(renderHwnd, 0x0202, IntPtr.Zero, lParam);
    }

    public static IntPtr FindRenderSurface(IntPtr parentHwnd) {
        IntPtr r = FindWindowEx(parentHwnd, IntPtr.Zero, "Chrome_RenderWidgetHostHWND", null);
        if (r != IntPtr.Zero) return r;
        IntPtr child = FindWindowEx(parentHwnd, IntPtr.Zero, null, null);
        while (child != IntPtr.Zero) {
            r = FindWindowEx(child, IntPtr.Zero, "Chrome_RenderWidgetHostHWND", null);
            if (r != IntPtr.Zero) return r;
            child = FindWindowEx(parentHwnd, child, null, null);
        }
        return parentHwnd;
    }

    public static List<IntPtr> GetVSCodeWindows() {
        var result = new List<IntPtr>();
        EnumWindows((hWnd, lParam) => {
            if (IsWindowVisible(hWnd)) {
                int len = GetWindowTextLength(hWnd);
                if (len > 0) {
                    var sb = new StringBuilder(len + 1);
                    GetWindowText(hWnd, sb, sb.Capacity);
                    if (sb.ToString().Contains("Visual Studio Code"))
                        result.Add(hWnd);
                }
            }
            return true;
        }, IntPtr.Zero);
        return result;
    }
}
"@

# --- Load config ---
$orchConfig = Get-Content "D:\Prospects\ScreenMemory\data\orchestrator.json" | ConvertFrom-Json
$orchHwnd = [IntPtr]$orchConfig.orchestrator_hwnd

# --- Find existing chat windows (detached, not the main editor) ---
$allWindows = [Ghost]::GetVSCodeWindows()
$chatWindows = @()
foreach ($hwnd in $allWindows) {
    if ($hwnd -eq $orchHwnd) { continue }
    $r = New-Object Ghost+RECT
    [Ghost]::GetWindowRect($hwnd, [ref]$r)
    $w = $r.Right - $r.Left
    # Detached chat windows are smaller than the main editor (< 1200px wide)
    if ($w -lt 1200) {
        $chatWindows += $hwnd
    }
}

# --- Check if any existing chat window is empty (no first prompt) ---
foreach ($cw in $chatWindows) {
    $cwRoot = [System.Windows.Automation.AutomationElement]::FromHandle($cw)
    # Conversation messages are ListItem with class 'monaco-list-row request/response'
    $listItems = $cwRoot.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::ListItem
        ))
    )
    $chatMessages = 0
    foreach ($li in $listItems) {
        if ($li.Current.ClassName -match 'monaco-list-row') {
            $chatMessages++
        }
    }
    $isEmpty = ($chatMessages -eq 0)

    if ($isEmpty) {
        Write-Host "BLOCKED: Chat window HWND=$cw has no first prompt yet. Use it before opening another."
        # Bring the empty window to attention
        [Ghost]::SetForegroundWindow($cw)
        Start-Sleep -Milliseconds 300
        [Ghost]::SetForegroundWindow($orchHwnd)
        exit 0
    }
}

# --- Calculate non-overlapping position (2x2 grid on right monitor) ---
# Right monitor: 1920,0 to 3840,1080. Taskbar ~40px at bottom -> usable y_max = 1040
# Grid: 930x500 windows (capped so bottom row ends at 1040), 2 columns x 2 rows
$gridW = 930
$gridH = 500
$gridSlots = @(
    @{X=1930; Y=20},    # top-left    (y+h = 520)
    @{X=2870; Y=20},    # top-right   (y+h = 520)
    @{X=1930; Y=540},   # bottom-left (y+h = 1040, taskbar safe)
    @{X=2870; Y=540}    # bottom-right(y+h = 1040, taskbar safe)
)

# Override window size for grid
$Width = $gridW
$Height = $gridH

# Collect occupied rects on right monitor
$occupiedRects = @()
foreach ($cw in $chatWindows) {
    $r = New-Object Ghost+RECT
    [Ghost]::GetWindowRect($cw, [ref]$r)
    if ($r.Left -ge 1900) {
        $occupiedRects += @{ Left=$r.Left; Top=$r.Top; Right=$r.Right; Bottom=$r.Bottom }
    }
}

# Find first unoccupied grid slot
$placed = $false
$newX = $gridSlots[0].X
$newY = $gridSlots[0].Y

foreach ($slot in $gridSlots) {
    $overlaps = $false
    foreach ($occ in $occupiedRects) {
        if ($slot.X -lt $occ.Right -and ($slot.X + $gridW) -gt $occ.Left -and
            $slot.Y -lt $occ.Bottom -and ($slot.Y + $gridH) -gt $occ.Top) {
            $overlaps = $true
            break
        }
    }
    if (-not $overlaps) {
        $newX = $slot.X
        $newY = $slot.Y
        $placed = $true
        break
    }
}

if (-not $placed) {
    Write-Host "BLOCKED: All 4 grid slots occupied. Close a chat window first."
    [Ghost]::SetForegroundWindow($orchHwnd)
    exit 0
}

# --- Snapshot windows before creation ---
$before = [Ghost]::GetVSCodeWindows()

# --- Open dropdown menu via ghost click ---
$editorRender = [Ghost]::FindRenderSurface($orchHwnd)
[Ghost]::SetForegroundWindow($orchHwnd)
Start-Sleep -Milliseconds 300

$root = [System.Windows.Automation.AutomationElement]::FromHandle($orchHwnd)

# Strategy: find the narrow "New Chat" dropdown button (≤20px wide)
# This exists in panel chat view. In editor chat view we need a different approach.
$buttons = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button
    ))
)

$dropdown = $null
$dropdownW = 999
foreach ($btn in $buttons) {
    $n = $btn.Current.Name
    $w = [int]$btn.Current.BoundingRectangle.Width
    $h = [int]$btn.Current.BoundingRectangle.Height
    # The ▾ dropdown is the NARROWEST button named "New Chat"
    if ($n -eq 'New Chat' -and $w -gt 0 -and $h -gt 15 -and $w -lt $dropdownW) {
        $dropdown = $btn
        $dropdownW = $w
    }
}

if (-not $dropdown) {
    Record-Failure "Could not find New Chat dropdown button"
    Write-Host "ERROR: Could not find New Chat dropdown button"
    exit 1
}

$dr = $dropdown.Current.BoundingRectangle
[Ghost]::Click($editorRender, [int]($dr.X + $dr.Width/2), [int]($dr.Y + $dr.Height/2))
Start-Sleep -Milliseconds 1500

# --- Click "New Chat Window" via UIA InvokePattern ---
$desktop = [System.Windows.Automation.AutomationElement]::RootElement
$menuItems = $desktop.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::MenuItem
    ))
)

$launched = $false
foreach ($mi in $menuItems) {
    if ($mi.Current.Name -eq 'New Chat Window') {
        $inv = $mi.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $inv.Invoke()
        $launched = $true
        break
    }
}

if (-not $launched) {
    Record-Failure "Could not find New Chat Window menu item"
    Write-Host "ERROR: Could not find 'New Chat Window' menu item"
    exit 1
}

Start-Sleep -Milliseconds 4000

# --- Find the new window ---
$after = [Ghost]::GetVSCodeWindows()
$newHwnd = $null
foreach ($hwnd in $after) {
    if ($before -notcontains $hwnd) {
        $newHwnd = $hwnd
        break
    }
}

if (-not $newHwnd) {
    Record-Failure "New chat window not detected"
    Write-Host "ERROR: New chat window not detected"
    exit 1
}

# --- Position without overlap ---
[Ghost]::MoveWindow($newHwnd, $newX, $newY, $Width, $Height, $true)

# --- Configure session target to Copilot CLI (invisible) ---
$newRender = [Ghost]::FindRenderSurface($newHwnd)
[Ghost]::SetForegroundWindow($newHwnd)
Start-Sleep -Milliseconds 2000

$chatRoot = [System.Windows.Automation.AutomationElement]::FromHandle($newHwnd)
$chatButtons = $chatRoot.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button
    ))
)

$needsConfig = $false
foreach ($btn in $chatButtons) {
    if ($btn.Current.Name -match 'Session Target' -and $btn.Current.Name -notmatch 'Copilot CLI') {
        $r = $btn.Current.BoundingRectangle
        [Ghost]::Click($newRender, [int]($r.X + $r.Width/2), [int]($r.Y + $r.Height/2))
        $needsConfig = $true
        break
    }
}

if ($needsConfig) {
    Start-Sleep -Milliseconds 1500

    $checkboxes = $desktop.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::CheckBox
        ))
    )
    foreach ($cb in $checkboxes) {
        if ($cb.Current.Name -eq 'Copilot CLI' -and $cb.Current.BoundingRectangle.Width -gt 0) {
            $tog = $cb.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
            $tog.Toggle()
            break
        }
    }
    Start-Sleep -Milliseconds 1000
}

# --- MODEL GUARD: Ensure Claude Opus 4.6 (fast mode) — max 2 attempts ---
for ($modelAttempt = 1; $modelAttempt -le 2; $modelAttempt++) {
    Start-Sleep -Milliseconds 500
    $chatRootM = [System.Windows.Automation.AutomationElement]::FromHandle($newHwnd)
    $mButtons = $chatRootM.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button
        ))
    )

    $pickModelBtn = $null
    $currentModelName = ""
    foreach ($btn in $mButtons) {
        if ($btn.Current.Name -match 'Pick Model') {
            $pickModelBtn = $btn
            $currentModelName = $btn.Current.Name
            break
        }
    }

    # Already correct or no button found — done
    if (-not $pickModelBtn -or $currentModelName -match 'Opus.*fast') {
        if ($pickModelBtn) { Write-Host "MODEL_GUARD: OK -- $currentModelName" }
        break
    }

    Write-Host "MODEL_GUARD: Wrong model '$currentModelName' -- attempt $modelAttempt/2..."
    [Ghost]::SetForegroundWindow($newHwnd)
    Start-Sleep -Milliseconds 300

    # Click Pick Model button to open quickpick
    $pmr = $pickModelBtn.Current.BoundingRectangle
    [Ghost]::SetForegroundWindow($newHwnd)
    Start-Sleep -Milliseconds 400
    [Ghost]::Click($newRender, [int]($pmr.X + $pmr.Width/2), [int]($pmr.Y + $pmr.Height/2))
    Start-Sleep -Milliseconds 2000

    # Type "fast" to filter to Opus fast mode -- confirmed working method
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
    [System.Windows.Forms.SendKeys]::SendWait("fast")
    Start-Sleep -Milliseconds 1500

    # First result should be Opus fast -- Down+Enter to select
    [System.Windows.Forms.SendKeys]::SendWait("{DOWN}{ENTER}")
    Start-Sleep -Milliseconds 1000
    Write-Host "MODEL_GUARD: Selected Opus fast via keyboard filter"
}

# --- Verify ---
$chatRoot3 = [System.Windows.Automation.AutomationElement]::FromHandle($newHwnd)
$finalButtons = $chatRoot3.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button
    ))
)

$target = ""; $model = ""
foreach ($btn in $finalButtons) {
    $name = $btn.Current.Name
    if ($name -match 'Session Target|Delegate Session') { $target = $name }
    if ($name -match 'Pick Model') { $model = $name }
}

# Final model guard check — fail loudly if still wrong
if ($model -and $model -notmatch 'Opus.*fast') {
    Record-Failure "Model guard failed -- model is '$model'"
    Write-Host "ERROR: MODEL_GUARD_FAILED -- model is '$model', expected Claude Opus 4.6 (fast mode)"
}

# --- Restore orchestrator ---
[Ghost]::SetForegroundWindow($orchHwnd)

# --- Success: clear failure tracker ---
Clear-Failures

# --- Report ---
$cursor = New-Object Ghost+POINT
[Ghost]::GetCursorPos([ref]$cursor)
Write-Host "OK HWND=$newHwnd pos=$newX,$newY | $target | $model | cursor=$($cursor.X),$($cursor.Y)"
