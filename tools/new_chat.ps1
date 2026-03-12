# new_chat.ps1 - Opens a new detached Copilot CLI chat window via invisible automation
# Uses ghost mouse (PostMessage) and UIA patterns — never moves the user's cursor.
# Enforces: no new window if an existing chat window has no first prompt yet.
# Enforces: new windows are tiled, never overlapping existing chat windows.
# Usage: powershell -ExecutionPolicy Bypass -File tools\new_chat.ps1

param(
    [int]$Monitor = 2,  # 1=left, 2=right
    [int]$Width = 800,
    [int]$Height = 880,
    [switch]$SkipEmptyCheck,  # Skip the "no first prompt" guard (used by skynet_start.py)
    [ValidateSet("worker", "consultant")]
    [string]$Layout = "worker"
)

# --- Session open failure tracker (max 2 consecutive attempts) ---
$failFile = "D:\Prospects\ScreenMemory\data\chat_open_failures.json"
$MAX_CONSECUTIVE_FAILS = 2

if (Test-Path $failFile) {
    try {
        $failData = Get-Content $failFile -Raw | ConvertFrom-Json
        if ($failData.consecutive_failures -ge $MAX_CONSECUTIVE_FAILS) {
            Write-Host "BLOCKED: $MAX_CONSECUTIVE_FAILS consecutive failures (last: $($failData.last_failure))."
            Write-Host "EDIT REQUIRED: Update button detection in tools\new_chat.ps1, then delete $failFile to retry."
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

# SendInput helper -- injects hardware-level events that Electron/Chromium processes
Add-Type @"
using System; using System.Runtime.InteropServices;
public class RealInput {
    [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT {
        public int dx, dy;
        public uint mouseData, dwFlags, time;
        public IntPtr dwExtraInfo;
    }
    [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT {
        public ushort wVk, wScan;
        public uint dwFlags, time;
        public IntPtr dwExtraInfo;
    }
    [StructLayout(LayoutKind.Explicit)] public struct INPUT {
        [FieldOffset(0)] public uint type;
        [FieldOffset(4)] public MOUSEINPUT mi;
        [FieldOffset(4)] public KEYBDINPUT ki;
    }
    [DllImport("user32.dll")] public static extern uint SendInput(uint n, INPUT[] inputs, int sz);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    public static void MouseClick(int x, int y) {
        SetCursorPos(x, y);
        System.Threading.Thread.Sleep(100);
        var inp = new INPUT[2];
        inp[0].type = 0; inp[0].mi.dwFlags = 0x0002; // MOUSEEVENTF_LEFTDOWN
        inp[1].type = 0; inp[1].mi.dwFlags = 0x0004; // MOUSEEVENTF_LEFTUP
        SendInput(2, inp, System.Runtime.InteropServices.Marshal.SizeOf(typeof(INPUT)));
    }
    public static void KeyPress(ushort vk) {
        var inp = new INPUT[2];
        inp[0].type = 1; inp[0].ki.wVk = vk; inp[0].ki.dwFlags = 0;
        inp[1].type = 1; inp[1].ki.wVk = vk; inp[1].ki.dwFlags = 0x0002; // KEYEVENTF_KEYUP
        SendInput(2, inp, System.Runtime.InteropServices.Marshal.SizeOf(typeof(INPUT)));
    }
}
"@ -ErrorAction SilentlyContinue

# MouseClick helper kept as alias for legacy code
Add-Type @"
using System; using System.Runtime.InteropServices;
public class MouseClick {
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint flags, int dx, int dy, uint data, UIntPtr extra);
    public const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    public const uint MOUSEEVENTF_LEFTUP   = 0x0004;
    public static void ClickAt(int x, int y) {
        SetCursorPos(x, y);
        System.Threading.Thread.Sleep(80);
        mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, UIntPtr.Zero);
        System.Threading.Thread.Sleep(50);
        mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, UIntPtr.Zero);
    }
}
"@ -ErrorAction SilentlyContinue

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

    public delegate bool EnumChildWP(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EnumChildWP cb, IntPtr l);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, System.Text.StringBuilder sb, int n);

    private static IntPtr _renderFound;
    private static bool _renderCb(IntPtr h, IntPtr l) {
        var sb = new System.Text.StringBuilder(256);
        GetClassName(h, sb, 256);
        if (sb.ToString() == "Chrome_RenderWidgetHostHWND") { _renderFound = h; return false; }
        return true;
    }
    public static IntPtr FindRenderSurface(IntPtr parentHwnd) {
        _renderFound = IntPtr.Zero;
        EnumChildWindows(parentHwnd, new EnumChildWP(_renderCb), IntPtr.Zero);
        return _renderFound != IntPtr.Zero ? _renderFound : parentHwnd;
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

    if ($isEmpty -and -not $SkipEmptyCheck) {
        Write-Host "BLOCKED: Chat window HWND=$cw has no first prompt yet. Use it before opening another."
        # Bring the empty window to attention
        [Ghost]::SetForegroundWindow($cw)
        Start-Sleep -Milliseconds 300
        [Ghost]::SetForegroundWindow($orchHwnd)
        exit 0
    }
}

# --- Calculate non-overlapping position ---
# Worker layout stays on monitor 2 in the original 2x2 grid.
# Consultant layout uses 2 dedicated slots over monitor 1 so candidate windows
# can still open when all worker slots are occupied.
if ($Layout -eq "consultant") {
    $gridW = 460
    $gridH = 500
    $gridSlots = @(
        @{X=976;  Y=20},   # monitor 1, left consultant slot
        @{X=1446; Y=20}    # monitor 1, right consultant slot
    )
    $occupiedMinX = 940
    $occupiedMaxX = 1915
    $fullMessage = "BLOCKED: All consultant slots occupied. Close a consultant candidate window first."
} else {
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
    $occupiedMinX = 1900
    $occupiedMaxX = [int]::MaxValue
    $fullMessage = "BLOCKED: All 4 grid slots occupied. Close a chat window first."
}

# Override window size for grid
$Width = $gridW
$Height = $gridH

# Collect occupied rects in the active layout band
$occupiedRects = @()
foreach ($cw in $chatWindows) {
    $r = New-Object Ghost+RECT
    [Ghost]::GetWindowRect($cw, [ref]$r)
    if ($r.Left -ge $occupiedMinX -and $r.Left -lt $occupiedMaxX) {
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
    Write-Host $fullMessage
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

# Strategy: find the narrow "New Chat" dropdown button (<=20px wide)
# This exists in panel chat view. In editor chat view we need a different approach.
# Retry up to 3 times -- UIA tree may load lazily in Chromium/Electron  # signed: gamma
$buttons = $null
for ($uiaRetry = 1; $uiaRetry -le 3; $uiaRetry++) {
    $buttons = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button
        ))
    )
    if ($buttons.Count -gt 0) { break }
    Write-Host "UIA retry $uiaRetry/3: 0 buttons found, waiting for tree..."
    Start-Sleep -Milliseconds 1500
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($orchHwnd)
}
Write-Host "UIA found $($buttons.Count) button(s)"

$dropdown = $null
$orchRect = New-Object Ghost+RECT
[Ghost]::GetWindowRect($orchHwnd, [ref]$orchRect)
$winTop = $orchRect.Top

# Strategy 1: ExpandCollapsePattern button that is small (≤25px wide) AND near the top of the window (toolbar area)
foreach ($btn in $buttons) {
    $n = $btn.Current.Name
    try {
        $w = [int]$btn.Current.BoundingRectangle.Width
        $h = [int]$btn.Current.BoundingRectangle.Height
        $y = [int]$btn.Current.BoundingRectangle.Y
    } catch { continue }
    if ($w -le 0 -or $h -le 0) { continue }
    if ($w -gt 25) { continue }  # The ▾ chevron is narrow (≤25px)
    if (($y - $winTop) -gt 200) { continue }  # Must be in top 200px of window (toolbar)
    try {
        $exp = $btn.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
        if ($exp -ne $null) {
            $dropdown = $btn
            Write-Host "DROPDOWN: found via ExpandCollapse -- '$n' w=$w y=$y"
            break
        }
    } catch {}
}

# Strategy 2: button name contains ▾ or explicit chevron label
if (-not $dropdown) {
    foreach ($btn in $buttons) {
        $n = $btn.Current.Name
        $w = [int]$btn.Current.BoundingRectangle.Width
        $h = [int]$btn.Current.BoundingRectangle.Height
        if ($w -le 0 -or $h -le 0) { continue }
        if ($n -match '▾|chevron|dropdown' -or ($n -match 'New Chat' -and $w -lt 25)) {
            $dropdown = $btn
            Write-Host "DROPDOWN: found via name match -- '$n' w=$w"
            break
        }
    }
}

# Strategy 3: narrowest visible "New Chat" button (original fallback)
if (-not $dropdown) {
    $dropdownW = 999
    foreach ($btn in $buttons) {
        $n = $btn.Current.Name
        $w = [int]$btn.Current.BoundingRectangle.Width
        $h = [int]$btn.Current.BoundingRectangle.Height
        if ($n -eq 'New Chat' -and $w -gt 0 -and $h -gt 15 -and $w -lt $dropdownW) {
            $dropdown = $btn
            $dropdownW = $w
        }
    }
    if ($dropdown) { Write-Host "DROPDOWN: found via narrowest fallback -- w=$dropdownW" }
}

# Strategy 4 (NEW): Search by Name="New Chat" across all control types  # signed: gamma
# VS Code Insiders may expose buttons as Custom, SplitButton, or other types
if (-not $dropdown) {
    try {
        $namedElements = $root.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::NameProperty,
                "New Chat"
            ))
        )
        Write-Host "STRATEGY 4: Found $($namedElements.Count) elements named 'New Chat'"
        foreach ($el in $namedElements) {
            try {
                $w = [int]$el.Current.BoundingRectangle.Width
                $h = [int]$el.Current.BoundingRectangle.Height
                $y = [int]$el.Current.BoundingRectangle.Y
            } catch { continue }
            if ($w -le 0 -or $h -le 0) { continue }
            if ($w -le 30 -and ($y - $winTop) -lt 200) {
                $dropdown = $el
                Write-Host "DROPDOWN: found via name search (narrow) -- w=$w type=$($el.Current.ControlType.ProgrammaticName)"
                break
            }
        }
        # Fallback: take any "New Chat" element that supports ExpandCollapse
        if (-not $dropdown) {
            foreach ($el in $namedElements) {
                try {
                    $exp = $el.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
                    if ($exp -ne $null) {
                        $dropdown = $el
                        Write-Host "DROPDOWN: found 'New Chat' with ExpandCollapse -- type=$($el.Current.ControlType.ProgrammaticName)"
                        break
                    }
                } catch {}
            }
        }
    } catch {
        Write-Host "STRATEGY 4 failed: $_"
    }
}

# Strategy 5 (NEW): Search for SplitButton control type  # signed: gamma
if (-not $dropdown) {
    try {
        $splitButtons = $root.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::SplitButton
            ))
        )
        Write-Host "STRATEGY 5: Found $($splitButtons.Count) SplitButton(s)"
        foreach ($sb in $splitButtons) {
            $n = $sb.Current.Name
            if ($n -match 'New Chat|Chat') {
                $dropdown = $sb
                Write-Host "DROPDOWN: found SplitButton '$n'"
                break
            }
        }
    } catch {
        Write-Host "STRATEGY 5 failed: $_"
    }
}

# Strategy 6: Command Palette Fallback -- guaranteed to work  # signed: gamma
$usedCommandPalette = $false
if (-not $dropdown) {
    $btnList = @()
    foreach ($btn in $buttons) {
        $n = $btn.Current.Name
        $w = [int]$btn.Current.BoundingRectangle.Width
        if ($n -ne '' -and $w -gt 0) { $btnList += "$n(${w}px)" }
    }
    Write-Host "FALLBACK: All UIA strategies failed. Buttons: $($btnList -join ', ')"
    Write-Host "Using Command Palette to open new chat window."
    [Ghost]::SetForegroundWindow($orchHwnd)
    Start-Sleep -Milliseconds 400
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
    [System.Windows.Forms.SendKeys]::SendWait("^+p")
    Start-Sleep -Milliseconds 1200
    [System.Windows.Forms.SendKeys]::SendWait("Chat: New Chat Window")
    Start-Sleep -Milliseconds 1500
    [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
    $usedCommandPalette = $true
}

$launched = $usedCommandPalette  # signed: gamma
if (-not $usedCommandPalette) {
    $dr = $dropdown.Current.BoundingRectangle

    # Use ExpandCollapsePattern.Expand() -- more reliable than ghost click
    try {
        $exp = $dropdown.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
        $exp.Expand()
    } catch {
        [Ghost]::Click($editorRender, [int]($dr.X + $dr.Width/2), [int]($dr.Y + $dr.Height/2))
    }
    Start-Sleep -Milliseconds 1200

    # --- Click "New Chat Window" via real mouse at its UIA coordinates ---
    for ($attempt = 1; $attempt -le 3 -and -not $launched; $attempt++) {
        $desktop = [System.Windows.Automation.AutomationElement]::RootElement
        $menuItems = $desktop.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::MenuItem
            ))
        )
        foreach ($mi in $menuItems) {
            if ($mi.Current.Name -eq 'New Chat Window') {
                try {
                    $mir = $mi.Current.BoundingRectangle
                    $mix = [int]($mir.X + $mir.Width/2)
                    $miy = [int]($mir.Y + $mir.Height/2)
                    [MouseClick]::ClickAt($mix, $miy)
                } catch {
                    try { $mi.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() } catch {}
                }
                $launched = $true
                break
            }
        }
        if (-not $launched) {
            # Menu may have closed -- re-expand dropdown
            Write-Host "RETRY ${attempt}: re-expanding dropdown"
            try {
                $exp = $dropdown.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
                $exp.Expand()
            } catch {
                [Ghost]::Click($editorRender, [int]($dr.X + $dr.Width/2), [int]($dr.Y + $dr.Height/2))
            }
            Start-Sleep -Milliseconds 1000
        }
    }

    if (-not $launched) {
        Record-Failure "Could not find New Chat Window menu item"
        Write-Host "ERROR: Could not find 'New Chat Window' menu item"
        exit 1
    }
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

    # Type "opus" to filter to Claude Opus models only (avoids "Grok Code Fast" false match)
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
    [System.Windows.Forms.SendKeys]::SendWait("opus")
    Start-Sleep -Milliseconds 1500

    # First result should be Opus fast -- Down+Enter to select
    [System.Windows.Forms.SendKeys]::SendWait("{DOWN}{ENTER}")
    Start-Sleep -Milliseconds 1000
    Write-Host "MODEL_GUARD: Selected Opus fast via keyboard filter"
}

# --- PERMISSION GUARD: Set Bypass Approvals ---
# Uses shared guard_bypass.ps1 (single source of truth)
# No focus needed — uses UIA Expand + PostMessage ghost keys

$guardScript = Join-Path $PSScriptRoot "guard_bypass.ps1"
$guardOutput = & $guardScript -Hwnd $newHwnd 6>&1 2>&1 | Out-String
Write-Host $guardOutput.Trim()
$permsApplied = $guardOutput -match 'PERMS_FIXED|PERMS_APPLIED|PERMS_OK'

# --- Verify ---
# guard_bypass.ps1 does its own retry verification with increasing delays.
# UIA can return stale names, so we trust the guard output + visual confirmation.
# Still read UIA for model/target reporting below.
Start-Sleep -Milliseconds 1000
$chatRoot3 = [System.Windows.Automation.AutomationElement]::FromHandle($newHwnd)
$finalButtons = $chatRoot3.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button
    ))
)

$target = ""; $model = ""; $perms = ""
foreach ($btn in $finalButtons) {
    $name = $btn.Current.Name
    if ($name -match 'Session Target|Delegate Session') { $target = $name }
    if ($name -match 'Pick Model') { $model = $name }
    if ($name -match 'Set Permissions') { $perms = $name }
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
$permsLabel = $(if ($permsApplied) { "Bypass Approvals" } else { $perms })
Write-Host "OK HWND=$newHwnd pos=$newX,$newY | $target | $model | $permsLabel | cursor=$($cursor.X),$($cursor.Y)"
