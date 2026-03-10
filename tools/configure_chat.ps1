# configure_chat.ps1 - Model & mode guard for VS Code Copilot Chat windows
# Enforces: Claude Opus 4.6 (fast mode) on all chat windows
# Usage: powershell -ExecutionPolicy Bypass -File tools\configure_chat.ps1 [-Hwnd <hwnd>]
#        Run without -Hwnd to scan and fix all chat windows.

param(
    [long]$Hwnd = 0,         # Specific window HWND (0 = scan all)
    [int]$MaxAttempts = 2    # Max attempts to fix model per window
)

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public class ChatGuard {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] public static extern IntPtr FindWindowEx(IntPtr hwndParent, IntPtr hwndChildAfter, string lpszClass, string lpszWindow);
    [DllImport("user32.dll")] public static extern bool ScreenToClient(IntPtr hWnd, ref POINT lpPoint);
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

function Enforce-Model([IntPtr]$windowHwnd) {
    $render = [ChatGuard]::FindRenderSurface($windowHwnd)

    for ($modelAttempt = 1; $modelAttempt -le $MaxAttempts; $modelAttempt++) {
        $root = [System.Windows.Automation.AutomationElement]::FromHandle($windowHwnd)
        $buttons = $root.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::Button
            ))
        )

        $pickModelBtn = $null
        $currentModel = ""
        foreach ($btn in $buttons) {
            if ($btn.Current.Name -match 'Pick Model') {
                $pickModelBtn = $btn
                $currentModel = $btn.Current.Name
                break
            }
        }

        if (-not $pickModelBtn) {
            Write-Host "SKIP HWND=$windowHwnd — no Pick Model button found"
            return $true
        }

        if ($currentModel -match 'Opus.*fast') {
            Write-Host "OK HWND=$windowHwnd — $currentModel"
            return $true
        }

        Write-Host "FIX HWND=$windowHwnd — wrong model: $currentModel (attempt $modelAttempt/$MaxAttempts)"
        [ChatGuard]::SetForegroundWindow($windowHwnd)
        Start-Sleep -Milliseconds 300

        # Click Pick Model button to open quickpick
        $pmr = $pickModelBtn.Current.BoundingRectangle
        [ChatGuard]::Click($render, [int]($pmr.X + $pmr.Width/2), [int]($pmr.Y + $pmr.Height/2))
        Start-Sleep -Milliseconds 2000

        # Search quickpick for Opus fast
        $qpRoot = [System.Windows.Automation.AutomationElement]::FromHandle($windowHwnd)
        $qpItems = $qpRoot.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::ListItem
            ))
        )

        $selected = $false
        foreach ($item in $qpItems) {
            if ($item.Current.Name -match 'Opus.*fast') {
                try {
                    $inv = $item.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
                    $inv.Invoke()
                    $selected = $true
                } catch {
                    $ir = $item.Current.BoundingRectangle
                    if ($ir.Width -gt 0) {
                        [ChatGuard]::Click($render, [int]($ir.X + $ir.Width/2), [int]($ir.Y + $ir.Height/2))
                        $selected = $true
                    }
                }
                Write-Host "  Selected: $($item.Current.Name)"
                break
            }
        }

        if (-not $selected) {
            # Dismiss quickpick with Escape
            [ChatGuard]::PostMessage($render, 0x0100, [IntPtr]0x1B, [IntPtr]0)
            Start-Sleep -Milliseconds 300
            Write-Host "  Opus fast not found in picker list"
            continue
        }
        Start-Sleep -Milliseconds 1000
    }

    # Final verification
    $vRoot = [System.Windows.Automation.AutomationElement]::FromHandle($windowHwnd)
    $vButtons = $vRoot.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button
        ))
    )
    foreach ($btn in $vButtons) {
        if ($btn.Current.Name -match 'Pick Model') {
            if ($btn.Current.Name -match 'Opus.*fast') {
                Write-Host "FIXED HWND=$windowHwnd — $($btn.Current.Name)"
                return $true
            }
            Write-Host "FAILED HWND=$windowHwnd — still: $($btn.Current.Name)"
            return $false
        }
    }
    return $false
}

# --- Main ---
$orchHwnd = [IntPtr]::Zero
if (Test-Path "D:\Prospects\ScreenMemory\data\orchestrator.json") {
    $orchConfig = Get-Content "D:\Prospects\ScreenMemory\data\orchestrator.json" | ConvertFrom-Json
    $orchHwnd = [IntPtr]$orchConfig.orchestrator_hwnd
}

if ($Hwnd -gt 0) {
    $result = Enforce-Model ([IntPtr]$Hwnd)
    if ($orchHwnd -ne [IntPtr]::Zero) { [ChatGuard]::SetForegroundWindow($orchHwnd) }
    if ($result) { exit 0 } else { exit 1 }
}

# Scan all chat windows (exclude orchestrator)
$allWindows = [ChatGuard]::GetVSCodeWindows()
$ok = 0; $failed = 0

foreach ($hwnd in $allWindows) {
    if ($hwnd -eq $orchHwnd) { continue }
    $r = New-Object ChatGuard+RECT
    [ChatGuard]::GetWindowRect($hwnd, [ref]$r)
    $w = $r.Right - $r.Left
    if ($w -lt 1200) {
        if (Enforce-Model $hwnd) { $ok++ } else { $failed++ }
    }
}

if ($orchHwnd -ne [IntPtr]::Zero) { [ChatGuard]::SetForegroundWindow($orchHwnd) }
Write-Host "Model guard complete: $ok ok, $failed failed"
if ($failed -gt 0) { exit 1 } else { exit 0 }
