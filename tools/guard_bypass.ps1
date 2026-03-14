# guard_bypass.ps1 - Single source of truth for setting Bypass/Autopilot permissions
# Uses UIA to detect current state + pyautogui for Chromium interaction.
# PostMessage/ghost keys do NOT work on Chromium overlays (INCIDENT 013).
#
# Called by: new_chat.ps1 and skynet_start.py
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\guard_bypass.ps1 -Hwnd <int>
#
# Output: PERMS_OK | PERMS_FIXED | PERMS_FAILED
# signed: orchestrator

param(
    [Parameter(Mandatory=$true)]
    [long]$Hwnd
)

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

function Find-PermissionButton {
    param([IntPtr]$targetHwnd)
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($targetHwnd)
    $btns = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button
        ))
    )
    foreach ($btn in $btns) {
        $n = $btn.Current.Name
        if ($n -match 'Permissions|Autopilot|Approvals') {
            return @{
                Button = $btn
                Name   = $n
                IsOK   = ($n -match 'Autopilot|Bypass')
                Rect   = $btn.Current.BoundingRectangle
            }
        }
    }
    return $null
}

$targetHwnd = [IntPtr]$Hwnd
$maxAttempts = 3

for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
    Write-Host "PERMS_CHECK attempt $attempt/$maxAttempts..."

    # --- Scan for permission button via UIA ---
    $perm = Find-PermissionButton -targetHwnd $targetHwnd

    if (-not $perm) {
        Write-Host "PERMS_NO_BUTTON (attempt $attempt)"
        if ($attempt -lt $maxAttempts) {
            Start-Sleep -Seconds 2
            continue
        }
        Write-Host "PERMS_FAILED"
        exit 1
    }

    if ($perm.IsOK) {
        Write-Host "PERMS_OK"
        exit 0
    }

    Write-Host "PERMS_CURRENT: $($perm.Name) -- switching to Autopilot..."

    # --- Use pyautogui to click the permission button (INCIDENT 013 fix) ---
    # Chromium quickpick overlays only respond to hardware-level input
    $rect = $perm.Rect
    $cx = [int]($rect.X + $rect.Width / 2)
    $cy = [int]($rect.Y + $rect.Height / 2)

    $pyScript = @"
import pyautogui, time
pyautogui.FAILSAFE = False
# Click the Permissions button to open dropdown
pyautogui.click($cx, $cy)
time.sleep(1.0)
# Select the bottom option (Autopilot/Bypass) -- it's below Default
pyautogui.press('down')
time.sleep(0.2)
pyautogui.press('down')
time.sleep(0.2)
pyautogui.press('enter')
time.sleep(0.8)
print('PYAUTOGUI_DONE')
"@

    $pyResult = python -c $pyScript 2>&1
    Write-Host "pyautogui result: $pyResult"

    # --- Verify the change via UIA ---
    Start-Sleep -Milliseconds 500
    $permAfter = Find-PermissionButton -targetHwnd $targetHwnd

    if ($permAfter -and $permAfter.IsOK) {
        Write-Host "PERMS_FIXED"
        exit 0
    }

    Write-Host "PERMS_VERIFY_FAILED attempt $attempt (still: $($permAfter.Name))"

    if ($attempt -lt $maxAttempts) {
        Start-Sleep -Seconds 2
    }
}

Write-Host "PERMS_FAILED"
exit 1
