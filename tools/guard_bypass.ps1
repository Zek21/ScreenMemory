# guard_bypass.ps1 - Single source of truth for setting Bypass Approvals
# Uses PostMessage ghost click + ghost keyboard (Down+Enter) to switch
# from "Default Approvals" to "Bypass Approvals" in VS Code Copilot chat.
#
# Requires: the Ghost C# class must already be loaded (Add-Type).
# Called by: new_chat.ps1 and skynet_start.py
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\guard_bypass.ps1 -Hwnd <int>
#   (standalone mode — loads its own Ghost class)
#
#   Or dot-source from new_chat.ps1 where Ghost is already loaded:
#   & .\tools\guard_bypass.ps1 -Hwnd $newHwnd

param(
    [Parameter(Mandatory=$true)]
    [long]$Hwnd
)

# --- Load Ghost class if not already present ---
try { [Ghost] | Out-Null } catch {
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    Add-Type @"
using System; using System.Runtime.InteropServices; using System.Text;
using System.Collections.Generic;
public class Ghost {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool ScreenToClient(IntPtr hWnd, ref POINT lpPoint);
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
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder sb, int n);
    private static IntPtr _renderFound;
    private static bool _renderCb(IntPtr h, IntPtr l) {
        var sb = new StringBuilder(256);
        GetClassName(h, sb, 256);
        if (sb.ToString() == "Chrome_RenderWidgetHostHWND") { _renderFound = h; return false; }
        return true;
    }
    public static IntPtr FindRenderSurface(IntPtr parentHwnd) {
        _renderFound = IntPtr.Zero;
        EnumChildWindows(parentHwnd, new EnumChildWP(_renderCb), IntPtr.Zero);
        return _renderFound != IntPtr.Zero ? _renderFound : parentHwnd;
    }
}
"@
}

$targetHwnd = [IntPtr]$Hwnd

# --- Scan for permission button ---
$root = [System.Windows.Automation.AutomationElement]::FromHandle($targetHwnd)
$btns = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button
    ))
)

$permBtn = $null
foreach ($btn in $btns) {
    $n = $btn.Current.Name
    if ($n -eq 'Set Permissions - Bypass Approvals') {
        Write-Host "PERMS_OK"
        exit 0
    }
    if ($n -eq 'Set Permissions - Default Approvals') {
        $permBtn = $btn
    }
}

if (-not $permBtn) {
    Write-Host "PERMS_NO_BUTTON"
    exit 0
}

# --- Switch from Default to Bypass ---
$render = [Ghost]::FindRenderSurface($targetHwnd)

# Step 1: Ghost click to open dropdown (NOT ExpandCollapsePattern which lies)
$pr = $permBtn.Current.BoundingRectangle
[Ghost]::Click($render, [int]($pr.X + $pr.Width/2), [int]($pr.Y + $pr.Height/2))
Start-Sleep -Milliseconds 1200

# Step 2: Ghost DOWN key (VK=0x28, scan=0x50) -- moves from Default to Bypass
[Ghost]::PostMessage($render, 0x0100, [IntPtr]0x28, [IntPtr]0x00500001) | Out-Null
Start-Sleep -Milliseconds 50
[Ghost]::PostMessage($render, 0x0101, [IntPtr]0x28, [IntPtr]::new(0xC0500001L)) | Out-Null
Start-Sleep -Milliseconds 200

# Step 3: Ghost ENTER key (VK=0x0D, scan=0x1C) -- selects Bypass
[Ghost]::PostMessage($render, 0x0100, [IntPtr]0x0D, [IntPtr]0x001C0001) | Out-Null
Start-Sleep -Milliseconds 50
[Ghost]::PostMessage($render, 0x0101, [IntPtr]0x0D, [IntPtr]::new(0xC01C0001L)) | Out-Null
Start-Sleep -Milliseconds 1500

# Step 4: Verify
$root2 = [System.Windows.Automation.AutomationElement]::FromHandle($targetHwnd)
$btns2 = $root2.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button
    ))
)
$verifyPerm = ($btns2 | Where-Object { $_.Current.Name -match 'Set Permissions' } | Select-Object -First 1).Current.Name
if ($verifyPerm -match 'Bypass') {
    Write-Host "PERMS_FIXED"
} else {
    Write-Host "PERMS_FAILED:$verifyPerm"
}
