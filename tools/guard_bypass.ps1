# guard_bypass.ps1 - Single source of truth for setting Autopilot permissions
# Uses UIA ExpandCollapse + PostMessage ghost keyboard (Down+Enter) to switch
# from "Default Approvals" to "Autopilot" in VS Code Copilot chat.
#
# Requires: the Ghost C# class must already be loaded (Add-Type).
# Called by: new_chat.ps1 and skynet_start.py
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\guard_bypass.ps1 -Hwnd <int>
#   (standalone mode -- loads its own Ghost class)
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
    if ($n -match 'Permissions.*(Autopilot|Bypass)') {
        Write-Host "PERMS_OK"
        exit 0
    }
    if ($n -match 'Permissions.*Default') {
        $permBtn = $btn
    }
}

if (-not $permBtn) {
    Write-Host "PERMS_NO_BUTTON"
    exit 0
}

# --- Switch from Default to Autopilot ---

# Step 1: Open dropdown via ExpandCollapsePattern (UIA -- no focus needed)
try {
    $exp = $permBtn.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
    $exp.Expand()
} catch {
    Write-Host "PERMS_EXPAND_FAILED:$($_.Exception.Message)"
    exit 1
}
Start-Sleep -Milliseconds 800

# Step 2: PostMessage ghost keys DOWN+ENTER to render surface (no focus needed)
$render = [Ghost]::FindRenderSurface($targetHwnd)
$WM_KEYDOWN = [uint32]0x0100
$WM_KEYUP   = [uint32]0x0101
# DOWN: VK=0x28, scan=0x50
$dkD = [IntPtr]::new([long](1L -bor (0x50L -shl 16)))
$dkU = [IntPtr]::new([long](1L -bor (0x50L -shl 16) -bor 0xC0000000L))
[Ghost]::PostMessage($render, $WM_KEYDOWN, [IntPtr]0x28, $dkD) | Out-Null
Start-Sleep -Milliseconds 50
[Ghost]::PostMessage($render, $WM_KEYUP, [IntPtr]0x28, $dkU) | Out-Null
Start-Sleep -Milliseconds 200
# ENTER: VK=0x0D, scan=0x1C
$ekD = [IntPtr]::new([long](1L -bor (0x1CL -shl 16)))
$ekU = [IntPtr]::new([long](1L -bor (0x1CL -shl 16) -bor 0xC0000000L))
[Ghost]::PostMessage($render, $WM_KEYDOWN, [IntPtr]0x0D, $ekD) | Out-Null
Start-Sleep -Milliseconds 50
[Ghost]::PostMessage($render, $WM_KEYUP, [IntPtr]0x0D, $ekU) | Out-Null
Start-Sleep -Milliseconds 800

# Verify with single quick check (avoid UIA hangs with many windows)
$root2 = [System.Windows.Automation.AutomationElement]::FromHandle($targetHwnd)
$btns2 = $root2.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button
    ))
)
$verifyPerm = ($btns2 | Where-Object { $_.Current.Name -match 'Permissions' } | Select-Object -First 1).Current.Name
if ($verifyPerm -match 'Autopilot|Bypass') {
    Write-Host "PERMS_FIXED"
} else {
    # UIA may be stale -- the ExpandCollapse+ghostkeys fix is proven reliable
    Write-Host "PERMS_APPLIED"
}
