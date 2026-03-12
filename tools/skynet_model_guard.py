#!/usr/bin/env python3
"""
skynet_model_guard.py -- Standalone model drift detection and correction.

Ensures all Skynet chat windows (workers + orchestrator) are on
Claude Opus 4.6 (fast mode) + Copilot CLI. Detects drift and auto-corrects
via UIA: opens Pick Model picker, types 'opus 4.6' to filter, Down+Enter to select.

Usage:
    python tools/skynet_model_guard.py --check HWND       # report model status
    python tools/skynet_model_guard.py --fix HWND         # fix model drift
    python tools/skynet_model_guard.py --check-all        # check all windows
    python tools/skynet_model_guard.py --fix-all          # fix all drifted windows

Importable:
    from tools.skynet_model_guard import check_model, fix_model, fix_all
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WORKERS_FILE = DATA_DIR / "workers.json"
ORCH_FILE = DATA_DIR / "orchestrator.json"

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _hidden_subprocess_kwargs(**kwargs):
    merged = dict(kwargs)
    if sys.platform == "win32":
        merged["creationflags"] = merged.get("creationflags", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = merged.get("startupinfo")
        if startupinfo is None and hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            merged["startupinfo"] = startupinfo
    return merged


def _hidden_run(args, **kwargs):
    return subprocess.run(args, **_hidden_subprocess_kwargs(**kwargs))


def _get_all_hwnds():
    """Get all Skynet window HWNDs (workers + orchestrator) with names."""
    hwnds = {}
    try:
        data = json.loads(WORKERS_FILE.read_text(encoding="utf-8"))
        for w in data.get("workers", []):
            name = w.get("name", "?")
            hwnd = w.get("hwnd", 0)
            if hwnd:
                hwnds[name] = int(hwnd)
    except Exception:
        pass
    try:
        data = json.loads(ORCH_FILE.read_text(encoding="utf-8"))
        hwnd = data.get("orchestrator_hwnd", 0)
        if hwnd:
            hwnds["orchestrator"] = int(hwnd)
    except Exception:
        pass
    return hwnds


def _get_orch_hwnd():
    try:
        data = json.loads(ORCH_FILE.read_text(encoding="utf-8"))
        return data.get("orchestrator_hwnd", 0)
    except Exception:
        return 0


# PowerShell snippet that checks model + target state via UIA button labels
_CHECK_PS = '''
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$hwnd=[IntPtr]{hwnd}
try {{
    $root=[System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
    $btns=$root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button)))
    $modelName="UNKNOWN"; $modelOk=$false; $targetOk=$false
    foreach($b in $btns) {{
        $n=$b.Current.Name
        if($n -match 'Pick Model') {{
            $modelName=$n
            if($n -match 'Opus 4\\.6.*fast') {{ $modelOk=$true }}
        }}
        if($n -match 'Copilot CLI') {{ $targetOk=$true }}
    }}
    Write-Host "MODEL_NAME:$modelName"
    Write-Host "MODEL_OK:$modelOk"
    Write-Host "TARGET_OK:$targetOk"
}} catch {{
    Write-Host "ERROR:$($_.Exception.Message)"
}}
'''

# PowerShell snippet that fixes model + target via UIA interaction
_FIX_PS = '''
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms
Add-Type @"
using System; using System.Runtime.InteropServices;
public class MGuard {{
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr wp, IntPtr lp);
    [DllImport("user32.dll")] public static extern IntPtr FindWindowEx(IntPtr p, IntPtr a, string c, string w);
    [DllImport("user32.dll")] public static extern bool ScreenToClient(IntPtr h, ref POINT p);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [StructLayout(LayoutKind.Sequential)] public struct POINT {{ public int X, Y; }}
    public static void Click(IntPtr rh, int sx, int sy) {{
        POINT pt; pt.X=sx; pt.Y=sy; ScreenToClient(rh,ref pt);
        IntPtr lp=(IntPtr)((pt.Y<<16)|(pt.X&0xFFFF));
        PostMessage(rh,0x0201,(IntPtr)1,lp); System.Threading.Thread.Sleep(50);
        PostMessage(rh,0x0202,IntPtr.Zero,lp);
    }}
    public static IntPtr FindRender(IntPtr p) {{
        IntPtr r=FindWindowEx(p,IntPtr.Zero,"Chrome_RenderWidgetHostHWND",null);
        if(r!=IntPtr.Zero)return r;
        IntPtr c=FindWindowEx(p,IntPtr.Zero,null,null);
        while(c!=IntPtr.Zero){{r=FindWindowEx(c,IntPtr.Zero,"Chrome_RenderWidgetHostHWND",null);if(r!=IntPtr.Zero)return r;c=FindWindowEx(p,c,null,null);}}
        return p;
    }}
}}
"@

$hwnd=[IntPtr]{hwnd}
$orchHwnd=[IntPtr]{orch_hwnd}
$render=[MGuard]::FindRender($hwnd)
$root=[System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
$btns=$root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button)))

function Get-VisibleEdit([System.Windows.Automation.AutomationElement]$rootEl) {{
    if($null -eq $rootEl) {{ return $null }}
    $edits = $rootEl.FindAll([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Edit)))
    foreach($edit in $edits) {{
        try {{
            $r = $edit.Current.BoundingRectangle
            if($r.Width -gt 0 -and $r.Height -gt 0) {{ return $edit }}
        }} catch {{}}
    }}
    return $null
}}

$modelOk=$false; $targetOk=$false
foreach($b in $btns) {{
    $n=$b.Current.Name
    if($n -match 'Pick Model.*Opus 4\\.6.*fast') {{ $modelOk=$true }}
    if($n -match 'Copilot CLI') {{ $targetOk=$true }}
}}

if(-not $modelOk) {{
    foreach($b in $btns) {{
        if($b.Current.Name -match 'Pick Model') {{
            [MGuard]::SetForegroundWindow($hwnd)
            Start-Sleep -Milliseconds 300
            $r=$b.Current.BoundingRectangle
            [MGuard]::Click($render,[int]($r.X+$r.Width/2),[int]($r.Y+$r.Height/2))
            Start-Sleep -Milliseconds 600
            $edit = $null
            for($i = 0; $i -lt 10 -and $null -eq $edit; $i++) {{
                $rootCheck=[System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
                $edit = Get-VisibleEdit $rootCheck
                if($null -eq $edit) {{ Start-Sleep -Milliseconds 150 }}
            }}
            if($null -eq $edit) {{
                Write-Host "MODEL_PICKER_NOT_READY"
                break
            }}
            try {{
                $vp = $edit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
                $vp.SetValue("opus 4.6")  # signed: orchestrator
            }} catch {{
                try {{ $edit.SetFocus() }} catch {{}}
                Start-Sleep -Milliseconds 100
                [System.Windows.Forms.SendKeys]::SendWait("^a")
                Start-Sleep -Milliseconds 50
                [System.Windows.Forms.SendKeys]::SendWait("opus 4.6")  # signed: orchestrator
            }}
            Start-Sleep -Milliseconds 500
            try {{ $edit.SetFocus() }} catch {{}}
            Start-Sleep -Milliseconds 100
            if([MGuard]::GetForegroundWindow() -ne $hwnd) {{
                Write-Host "MODEL_SELECTION_ABORTED_FOCUS_LOST"
                break
            }}
            [System.Windows.Forms.SendKeys]::SendWait("{{DOWN}}{{ENTER}}")
            Start-Sleep -Milliseconds 800
            $rootVerify=[System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
            $btnsVerify=$rootVerify.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                (New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                    [System.Windows.Automation.ControlType]::Button)))
            $modelFixed = $false
            foreach($vb in $btnsVerify) {{
                if($vb.Current.Name -match 'Pick Model.*Opus 4\\.6.*fast') {{
                    $modelFixed = $true
                    break
                }}
            }}
            if($modelFixed) {{
                Write-Host "MODEL_FIXED"
            }} else {{
                Write-Host "MODEL_FIX_UNVERIFIED"
            }}
            break
        }}
    }}
}}

if(-not $targetOk) {{
    $root2=[System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
    $btns2=$root2.FindAll([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button)))
    foreach($b in $btns2) {{
        if($b.Current.Name -match 'Session Target' -and $b.Current.Name -notmatch 'Copilot CLI') {{
            $r=$b.Current.BoundingRectangle
            [MGuard]::Click($render,[int]($r.X+$r.Width/2),[int]($r.Y+$r.Height/2))
            Start-Sleep -Milliseconds 1500
            $desktop=[System.Windows.Automation.AutomationElement]::RootElement
            $cbs=$desktop.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                (New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                    [System.Windows.Automation.ControlType]::CheckBox)))
            foreach($cb in $cbs) {{
                if($cb.Current.Name -eq 'Copilot CLI' -and $cb.Current.BoundingRectangle.Width -gt 0) {{
                    $cb.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern).Toggle()
                    Write-Host "TARGET_FIXED"
                    break
                }}
            }}
            Start-Sleep -Milliseconds 800
            break
        }}
    }}
}}

if($modelOk -and $targetOk) {{ Write-Host "GUARD_OK" }}
[MGuard]::SetForegroundWindow($orchHwnd)
'''


def check_model(hwnd):
    """Check model status for a window. Returns dict with model_ok, target_ok, model_name."""
    ps = _CHECK_PS.format(hwnd=int(hwnd))
    try:
        r = _hidden_run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15
        )
        out = r.stdout
        result = {"hwnd": int(hwnd), "model_ok": False, "target_ok": False, "model_name": "UNKNOWN", "error": None}

        for line in out.splitlines():
            line = line.strip()
            if line.startswith("MODEL_NAME:"):
                result["model_name"] = line.split(":", 1)[1]
            elif line.startswith("MODEL_OK:"):
                result["model_ok"] = line.split(":", 1)[1].strip().lower() == "true"
            elif line.startswith("TARGET_OK:"):
                result["target_ok"] = line.split(":", 1)[1].strip().lower() == "true"
            elif line.startswith("ERROR:"):
                result["error"] = line.split(":", 1)[1]

        return result
    except Exception as e:
        return {"hwnd": int(hwnd), "model_ok": False, "target_ok": False, "model_name": "ERROR", "error": str(e)}


def fix_model(hwnd, orch_hwnd=None):
    """Fix model drift for a window. Returns 'GUARD_OK', 'MODEL_FIXED', 'TARGET_FIXED', or 'FAILED'."""
    if orch_hwnd is None:
        orch_hwnd = _get_orch_hwnd() or hwnd

    ps = _FIX_PS.format(hwnd=int(hwnd), orch_hwnd=int(orch_hwnd))
    try:
        r = _hidden_run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=25
        )
        out = r.stdout
        actions = []
        if "MODEL_FIXED" in out:
            actions.append("MODEL_FIXED")
        if "TARGET_FIXED" in out:
            actions.append("TARGET_FIXED")
        if "GUARD_OK" in out:
            actions.append("GUARD_OK")
        if "MODEL_PICKER_NOT_READY" in out:
            actions.append("MODEL_PICKER_NOT_READY")
        if "MODEL_SELECTION_ABORTED_FOCUS_LOST" in out:
            actions.append("MODEL_SELECTION_ABORTED_FOCUS_LOST")
        if "MODEL_FIX_UNVERIFIED" in out:
            actions.append("MODEL_FIX_UNVERIFIED")
        return "+".join(actions) if actions else "NO_CHANGE"
    except Exception as e:
        return f"FAILED:{e}"


def check_all():
    """Check model status for all Skynet windows. Returns dict of name->status."""
    hwnds = _get_all_hwnds()
    results = {}
    for name, hwnd in hwnds.items():
        results[name] = check_model(hwnd)
        results[name]["name"] = name
    return results


def fix_all():
    """Fix model drift for all Skynet windows. Returns dict of name->action."""
    hwnds = _get_all_hwnds()
    orch_hwnd = _get_orch_hwnd()
    results = {}

    # Check first, only fix drifted ones
    for name, hwnd in hwnds.items():
        status = check_model(hwnd)
        if status["model_ok"] and status["target_ok"]:
            results[name] = "GUARD_OK"
            print(f"  {name}: OK ({status['model_name']})")
        else:
            drift = []
            if not status["model_ok"]:
                drift.append(f"model={status['model_name']}")
            if not status["target_ok"]:
                drift.append("target!=CLI")
            print(f"  {name}: DRIFT ({', '.join(drift)}) -- fixing...")
            action = fix_model(hwnd, orch_hwnd)
            results[name] = action
            print(f"  {name}: {action}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Skynet Model Guard -- detect and fix model drift")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", type=int, metavar="HWND", help="Check model status for a window")
    group.add_argument("--fix", type=int, metavar="HWND", help="Fix model drift for a window")
    group.add_argument("--check-all", action="store_true", help="Check all Skynet windows")
    group.add_argument("--fix-all", action="store_true", help="Fix all drifted windows")
    args = parser.parse_args()

    if args.check:
        result = check_model(args.check)
        ok = "OK" if result["model_ok"] and result["target_ok"] else "DRIFT"
        print(f"HWND {args.check}: {ok}")
        print(f"  Model: {result['model_name']} (ok={result['model_ok']})")
        print(f"  Target: Copilot CLI (ok={result['target_ok']})")
        if result.get("error"):
            print(f"  Error: {result['error']}")

    elif args.fix:
        orch_hwnd = _get_orch_hwnd()
        print(f"Fixing HWND {args.fix} (orch={orch_hwnd})...")
        action = fix_model(args.fix, orch_hwnd)
        print(f"Result: {action}")

    elif args.check_all:
        print("Checking all Skynet windows...")
        results = check_all()
        drifted = sum(1 for r in results.values() if not (r["model_ok"] and r["target_ok"]))
        print(f"\nTotal: {len(results)} windows, {drifted} drifted")

    elif args.fix_all:
        print("Fixing all drifted Skynet windows...")
        results = fix_all()
        fixed = sum(1 for v in results.values() if "FIXED" in v)
        ok = sum(1 for v in results.values() if v == "GUARD_OK")
        print(f"\nTotal: {len(results)} windows, {ok} already OK, {fixed} fixed")


if __name__ == "__main__":
    main()
