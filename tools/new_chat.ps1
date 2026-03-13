# new_chat.ps1 - Opens a new detached Copilot CLI chat window via invisible automation
# Uses UIA patterns (ExpandCollapsePattern, InvokePattern, TogglePattern) and
# PostMessage ghost clicks — never moves the user's cursor.
# Enforces: no new window if an existing chat window has no first prompt yet.
# Enforces: new windows are tiled, never overlapping existing chat windows.
# MUST be called with: powershell -STA -ExecutionPolicy Bypass -File tools\new_chat.ps1
# The -STA flag is required for UIA COM operations to work correctly.

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

# --- Set Autopilot permissions via UIA SetFocus + ExpandCollapsePattern + keyboard ---
# NO mouse movement. Uses SetFocus + ExpandCollapsePattern to open the dropdown,
# then keyboard DOWN+DOWN+ENTER to select Autopilot (3rd item).
# TogglePattern changes UIA state but doesn't commit in VS Code -- keyboard selection does.
# MUST run in STA thread (call with powershell -STA -File).
function Set-AutopilotPermissions {
    param([long]$Hwnd, [int]$TimeoutMs = 15000)
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
    $hwnd = [IntPtr]$Hwnd
    [Ghost]::SetForegroundWindow($hwnd)
    Start-Sleep -Milliseconds 500

    $root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
    if (-not $root) { return 'NO_ROOT' }

    # Find the permissions button
    $btnCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button
    )
    $btns = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $btnCond)
    $permBtn = $null
    foreach ($b in $btns) {
        if ($b.Current.Name -match 'Permissions') {
            $permBtn = $b
            break
        }
    }
    if (-not $permBtn) { return 'NO_PERMS_BUTTON' }
    if ($permBtn.Current.Name -match 'Autopilot') { return 'ALREADY_AUTOPILOT' }

    # SetFocus first (required for Expand to work from background context)
    $permBtn.SetFocus()
    Start-Sleep -Milliseconds 400

    # Open dropdown via ExpandCollapsePattern
    try {
        $ec = $permBtn.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
        $ec.Expand()
    } catch {
        return "EXPAND_FAILED:$($_.Exception.Message)"
    }
    Start-Sleep -Milliseconds 1200

    # Select Autopilot via keyboard: DOWN DOWN ENTER (Autopilot is the 3rd item)
    # Item order: Default Approvals, Bypass Approvals, Autopilot (Preview)
    [System.Windows.Forms.SendKeys]::SendWait("{DOWN}")
    Start-Sleep -Milliseconds 200
    [System.Windows.Forms.SendKeys]::SendWait("{DOWN}")
    Start-Sleep -Milliseconds 200
    [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
    Start-Sleep -Milliseconds 800
    return 'OK'
}

# --- UIA scan with timeout (prevents hangs with 4+ VS Code windows) ---
function Scan-ButtonsWithTimeout {
    param([long]$Hwnd, [int]$TimeoutMs = 12000)
    $rs = [runspacefactory]::CreateRunspace()
    $rs.ApartmentState = [System.Threading.ApartmentState]::STA
    $rs.Open()
    $ps = [powershell]::Create()
    $ps.Runspace = $rs
    [void]$ps.AddScript({
        param($h)
        Add-Type -AssemblyName UIAutomationClient
        $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$h)
        $btns = $root.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::Button
            ))
        )
        $data = @()
        foreach ($b in $btns) {
            $r = $b.Current.BoundingRectangle
            $data += [PSCustomObject]@{
                Name = $b.Current.Name
                CX = [int]($r.X + $r.Width/2)
                CY = [int]($r.Y + $r.Height/2)
            }
        }
        return $data
    })
    [void]$ps.AddArgument($Hwnd)
    $handle = $ps.BeginInvoke()
    $completed = $handle.AsyncWaitHandle.WaitOne($TimeoutMs)
    if ($completed) {
        $result = $ps.EndInvoke($handle)
        $ps.Dispose(); $rs.Dispose()
        return @($result)
    } else {
        $ps.Stop(); $ps.Dispose(); $rs.Dispose()
        return $null
    }
}

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
if (-not $SkipEmptyCheck) {
    foreach ($cw in $chatWindows) {
        # Use timeout-protected scan to avoid UIA hangs with many windows
        $listItemJob = Start-Job -ScriptBlock {
            param($h)
            Add-Type -AssemblyName UIAutomationClient
            $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$h)
            $items = $root.FindAll(
                [System.Windows.Automation.TreeScope]::Descendants,
                (New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                    [System.Windows.Automation.ControlType]::ListItem
                ))
            )
            $count = 0
            foreach ($li in $items) {
                if ($li.Current.ClassName -match 'monaco-list-row') { $count++ }
            }
            return $count
        } -ArgumentList ([long]$cw)

        $completed = $listItemJob | Wait-Job -Timeout 8
        if ($completed) {
            $chatMessages = Receive-Job $listItemJob
            Remove-Job $listItemJob
        } else {
            Stop-Job $listItemJob; Remove-Job $listItemJob
            $chatMessages = 1  # Assume not empty on timeout
        }

        if ($chatMessages -eq 0) {
            Write-Host "BLOCKED: Chat window HWND=$cw has no first prompt yet. Use it before opening another."
            [Ghost]::SetForegroundWindow($cw)
            Start-Sleep -Milliseconds 300
            [Ghost]::SetForegroundWindow($orchHwnd)
            exit 0
        }
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

# --- Open new chat window via UIA ExpandCollapsePattern + InvokePattern (zero mouse) ---
# Runs in STA runspace because UIA desktop scans require Single-Threaded Apartment
[Ghost]::SetForegroundWindow($orchHwnd)
Start-Sleep -Milliseconds 500

function Open-NewChatWindowUIA {
    param([long]$OrchHwnd)
    # Load UIA assemblies (main thread must be STA -- call with powershell -STA -File)
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes

    $hwnd = [IntPtr]$OrchHwnd
    $null = [Ghost]::SetForegroundWindow($hwnd)
    Start-Sleep -Milliseconds 500

    $root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
    if (-not $root) { return 'NO_ROOT' }

    # Targeted search: exact ClassName + Name + ControlType
    $chevronCond = New-Object System.Windows.Automation.AndCondition(
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button
        )),
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty,
            'New Chat'
        )),
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ClassNameProperty,
            'action-label codicon codicon-chevron-down'
        ))
    )
    $chevronBtn = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $chevronCond)
    # Fallback: any "New Chat" button with chevron in ClassName
    if (-not $chevronBtn) {
        $nameCond = New-Object System.Windows.Automation.AndCondition(
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::Button
            )),
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::NameProperty,
                'New Chat'
            ))
        )
        $matches = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $nameCond)
        foreach ($b in $matches) {
            if ($b.Current.ClassName -match 'chevron') {
                $chevronBtn = $b; break
            }
        }
    }
    if (-not $chevronBtn) { return 'NO_BUTTON' }

    # Expand dropdown via UIA ExpandCollapsePattern (no mouse)
    try {
        $chevronBtn.SetFocus()
        Start-Sleep -Milliseconds 300
        $ec = $chevronBtn.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
        $ec.Expand()
    } catch {
        try {
            $inv = $chevronBtn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
            $inv.Invoke()
        } catch {
            return "EXPAND_FAILED:$($_.Exception.Message)"
        }
    }
    Start-Sleep -Milliseconds 800

    # Find "New Chat Window" menu item via FocusedElement + ControlViewWalker (no keyboard needed)
    $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
    if (-not $focused) { return 'NO_FOCUSED' }
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $child = $walker.GetFirstChild($focused)
    $target = $null
    $idx = 0
    while ($child -ne $null -and $idx -lt 20) {
        if ($child.Current.Name -eq 'New Chat Window') {
            $target = $child
            break
        }
        $child = $walker.GetNextSibling($child)
        $idx++
    }
    if (-not $target) {
        try { $ec.Collapse() } catch {}
        return 'NO_MENU_ITEM'
    }

    # Invoke the menu item via InvokePattern (pure UIA, no mouse or keyboard)
    try {
        $menuInv = $target.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $menuInv.Invoke()
    } catch {
        try { $ec.Collapse() } catch {}
        return "INVOKE_FAILED:$($_.Exception.Message)"
    }
    return 'OK'
}

$dropdownResult = Open-NewChatWindowUIA -OrchHwnd ([long]$orchHwnd)
Write-Host "DROPDOWN: $dropdownResult"
if ($dropdownResult -ne 'OK') {
    Record-Failure "Dropdown open failed: $dropdownResult"
    Write-Host "ERROR: Could not open new chat window via dropdown: $dropdownResult"
    exit 1
}
$launched = $true

# --- Poll for new window (faster than fixed 4s wait) ---
$newHwnd = $null
for ($poll = 1; $poll -le 8; $poll++) {
    Start-Sleep -Milliseconds 500
    $after = [Ghost]::GetVSCodeWindows()
    foreach ($hwnd in $after) {
        if ($before -notcontains $hwnd) {
            $newHwnd = $hwnd
            break
        }
    }
    if ($newHwnd) { break }
}

# --- Find the new window ---
if (-not $newHwnd) {
    Record-Failure "New chat window not detected"
    Write-Host "ERROR: New chat window not detected"
    exit 1
}

# --- Position without overlap ---
[Ghost]::MoveWindow($newHwnd, $newX, $newY, $Width, $Height, $true)

# --- Configure: Session Target + Model + Permissions ---
$newRender = [Ghost]::FindRenderSurface($newHwnd)
[Ghost]::SetForegroundWindow($newHwnd)
Start-Sleep -Milliseconds 600
Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue

$target = "unknown"
$model = "unknown"
$permsLabel = "unknown"
$needsConfig = $false

for ($guardPass = 1; $guardPass -le 2; $guardPass++) {
    $buttons = Scan-ButtonsWithTimeout -Hwnd ([long]$newHwnd) -TimeoutMs 12000
    if (-not $buttons) {
        Write-Host "UIA_TIMEOUT: Button scan timed out (pass $guardPass)"
        break
    }

    # --- SESSION TARGET (pass 1 only) ---
    if ($guardPass -eq 1) {
        $stBtn = $buttons | Where-Object { $_.Name -match 'Session Target' } | Select-Object -First 1
        if ($stBtn) {
            if ($stBtn.Name -notmatch 'Copilot CLI') {
                # Open session target picker via UIA ExpandCollapsePattern (no mouse)
                $uiaNewRoot = [System.Windows.Automation.AutomationElement]::FromHandle($newHwnd)
                $uiaNewBtns = $uiaNewRoot.FindAll(
                    [System.Windows.Automation.TreeScope]::Descendants,
                    (New-Object System.Windows.Automation.PropertyCondition(
                        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                        [System.Windows.Automation.ControlType]::Button
                    ))
                )
                $stUia = $null
                foreach ($ub in $uiaNewBtns) {
                    if ($ub.Current.Name -match 'Session Target') { $stUia = $ub; break }
                }
                if ($stUia) {
                    try {
                        $stUia.SetFocus()
                        Start-Sleep -Milliseconds 300
                        $stExpand = $stUia.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
                        $stExpand.Expand()
                        Write-Host "SESSION_TARGET: Opened picker via UIA ExpandCollapsePattern"
                    } catch {
                        Write-Host "SESSION_TARGET: ExpandCollapse failed, trying Ghost.Click fallback"
                        $renderST = [Ghost]::FindRenderSurface($newHwnd)
                        [Ghost]::Click($renderST, $stBtn.CX, $stBtn.CY)
                    }
                    Start-Sleep -Milliseconds 600
                    [System.Windows.Forms.SendKeys]::SendWait("CLI")
                    Start-Sleep -Milliseconds 300
                    [System.Windows.Forms.SendKeys]::SendWait(" ")
                    Start-Sleep -Milliseconds 200
                    [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
                    Start-Sleep -Milliseconds 400
                    $target = "Set Session Target - Copilot CLI"
                    $needsConfig = $true
                }
            } else {
                $target = $stBtn.Name
            }
        }
    }

    # --- MODEL GUARD ---
    $pmBtn = $buttons | Where-Object { $_.Name -match 'Pick Model' } | Select-Object -First 1
    if ($pmBtn) {
        $model = $pmBtn.Name
        if ($model -notmatch 'Opus.*fast') {
            Write-Host "MODEL_GUARD: Wrong model '$model' -- attempt $guardPass/2..."
            # Open model picker via UIA ExpandCollapsePattern (no mouse)
            $uiaNewRoot = [System.Windows.Automation.AutomationElement]::FromHandle($newHwnd)
            $uiaNewBtns = $uiaNewRoot.FindAll(
                [System.Windows.Automation.TreeScope]::Descendants,
                (New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                    [System.Windows.Automation.ControlType]::Button
                ))
            )
            $pmUia = $null
            foreach ($ub in $uiaNewBtns) {
                if ($ub.Current.Name -match 'Pick Model') { $pmUia = $ub; break }
            }
            if ($pmUia) {
                try {
                    $pmUia.SetFocus()
                    Start-Sleep -Milliseconds 300
                    $pmExpand = $pmUia.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
                    $pmExpand.Expand()
                    Write-Host "MODEL_GUARD: Opened model picker via UIA ExpandCollapsePattern"
                } catch {
                    Write-Host "MODEL_GUARD: ExpandCollapse failed, trying Ghost.Click fallback"
                    $renderModel = [Ghost]::FindRenderSurface($newHwnd)
                    [Ghost]::Click($renderModel, $pmBtn.CX, $pmBtn.CY)
                }
            }
            Start-Sleep -Milliseconds 1200
            [System.Windows.Forms.SendKeys]::SendWait("fast")
            Start-Sleep -Milliseconds 800
            [System.Windows.Forms.SendKeys]::SendWait("{DOWN}{ENTER}")
            Start-Sleep -Milliseconds 500
            Write-Host "MODEL_GUARD: Selected Opus fast via keyboard filter"
            continue  # Re-scan to verify model stuck
        } else {
            Write-Host "MODEL_GUARD: OK -- $model"
        }
    }

    # --- PERMISSION GUARD (uses UIA TogglePattern on dropdown CheckBox items) ---
    $pBtn = $buttons | Where-Object { $_.Name -match 'Permissions' } | Select-Object -First 1
    if ($pBtn) {
        if ($pBtn.Name -match '(Autopilot|Bypass)') {
            $permsLabel = "Autopilot"
            Write-Host "PERMS_OK: $($pBtn.Name)"
        } elseif ($pBtn.Name -match 'Default') {
            $permsResult = Set-AutopilotPermissions -Hwnd ([long]$newHwnd)
            if ($permsResult -eq 'OK') {
                $permsLabel = "Autopilot (pending verify)"
                Write-Host "PERMS_APPLIED_PENDING_VERIFY"
            } else {
                $permsLabel = "FAILED"
                Write-Host "PERMS_APPLY_FAILED: $permsResult"
            }
        }
    }

    break  # Done — no continue means model was OK
}

# --- POST-GUARD VISUAL VERIFICATION (mandatory — never trust reported values) ---
$verifyButtons = Scan-ButtonsWithTimeout -Hwnd ([long]$newHwnd) -TimeoutMs 10000
if ($verifyButtons) {
    $vSession = $verifyButtons | Where-Object { $_.Name -match 'Session Target' } | Select-Object -First 1
    $vModel   = $verifyButtons | Where-Object { $_.Name -match 'Pick Model' } | Select-Object -First 1
    $vPerms   = $verifyButtons | Where-Object { $_.Name -match 'Permissions' } | Select-Object -First 1

    if ($vSession) { $target = $vSession.Name }
    if ($vModel)   { $model = $vModel.Name }
    if ($vPerms) {
        if ($vPerms.Name -match '(Autopilot|Bypass)') {
            $permsLabel = "Autopilot"
            Write-Host "PERMS_VERIFIED: $($vPerms.Name)"
        } else {
            # Retry permissions via UIA TogglePattern
            Write-Host "PERMS_RETRY: Still '$($vPerms.Name)' -- retrying via TogglePattern..."
            $retryResult = Set-AutopilotPermissions -Hwnd ([long]$newHwnd)
            Start-Sleep -Milliseconds 800

            # Re-verify after retry
            $retryButtons = Scan-ButtonsWithTimeout -Hwnd ([long]$newHwnd) -TimeoutMs 8000
            if ($retryButtons) {
                $rPerms = $retryButtons | Where-Object { $_.Name -match 'Permissions' } | Select-Object -First 1
                if ($rPerms -and $rPerms.Name -match '(Autopilot|Bypass)') {
                    $permsLabel = "Autopilot"
                    Write-Host "PERMS_RETRY_OK: $($rPerms.Name)"
                } else {
                    $permsLabel = "FAILED:$($rPerms.Name)"
                    Write-Host "PERMS_RETRY_FAILED: Still '$($rPerms.Name)'"
                }
            }
        }
    }

    Write-Host "VERIFY: session='$target' model='$model' perms='$permsLabel'"
} else {
    Write-Host "VERIFY_TIMEOUT: Could not re-scan buttons for verification"
}

# Final model guard check
if ($model -and $model -notmatch 'Opus.*fast' -and $model -ne 'unknown') {
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
Write-Host "OK HWND=$newHwnd pos=$newX,$newY | $target | $model | $permsLabel | cursor=$($cursor.X),$($cursor.Y)"
