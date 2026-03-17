# Orch-Start -- Skynet Orchestrator (GOD) bootstrap entry point
# Smart, non-blocking startup: checks what's running, starts only what's missing.
# If everything is already live, opens the dashboard instantly (<2s).
#
# Flags:
#   -Fresh         Force a full fresh boot (kills existing and restarts)
#   -Workers N     Number of workers to open (default: 4)
#   -Timeout N     Max wait for skynet_start.py (default: 120)
#   -SkipInfra     Skip infrastructure checks, go straight to worker windows only
#   -SkipWorkers   Skip worker window opening (infrastructure + daemons only)
param(
    [switch]$Fresh,
    [int]$Workers = 4,
    [int]$Timeout = 120,
    [switch]$SkipInfra,
    [switch]$SkipWorkers
)

$ErrorActionPreference = "Continue"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$dataDir  = Join-Path $repoRoot "data"

# Resolve Python (prefer venv)
$venvPython = Join-Path (Split-Path $repoRoot -Parent) "env\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }

# -- INCIDENT 016 Guard: Prevent cli.isolationOption.enabled drift --
# If this setting is True, CLI sessions are isolated and CANNOT delegate to workers.
# This silently breaks ALL dispatch/delegation. Check and fix at boot.
try {
    $userSettingsPath = Join-Path $env:APPDATA "Code - Insiders\User\settings.json"
    if (Test-Path $userSettingsPath) {
        $uSettings = Get-Content $userSettingsPath -Raw | ConvertFrom-Json
        $isoVal = $uSettings.'github.copilot.chat.cli.isolationOption.enabled'
        if ($isoVal -eq $true) {
            Write-Host "[ISOLATION GUARD] DANGER: cli.isolationOption.enabled=True -- fixing!"
            & $python tools/skynet_isolation_guard.py
        }
    }
} catch {
    Write-Host "[ISOLATION GUARD] Check failed: $_"
}

# -- Helpers --

function Write-Status($msg, $level = "INFO") {
    $ts = Get-Date -Format "HH:mm:ss"
    $prefix = switch ($level) {
        "OK"   { "[OK ]" }
        "WARN" { "[!!]" }
        "ERR"  { "[XX]" }
        "SYS"  { "[>>]" }
        default { "[--]" }
    }
    Write-Host "[$ts] $prefix $msg"
}

function Test-Port([int]$port, [int]$timeoutMs = 2000) {
    foreach ($loopbackHost in @("127.0.0.1", "localhost", "::1")) {
        $tcp = $null
        try {
            $tcp = New-Object System.Net.Sockets.TcpClient
            $ar  = $tcp.BeginConnect($loopbackHost, $port, $null, $null)
            $ok  = $ar.AsyncWaitHandle.WaitOne($timeoutMs, $false)
            if ($ok) { try { $tcp.EndConnect($ar) } catch {} }
            if ($ok) { return $true }
        } catch {
        } finally {
            if ($null -ne $tcp) { $tcp.Close() }
        }
    }
    return $false
}  # signed: consultant -- IPv6-only localhost bindings must not be reported as down

function Get-SkynetStatus {
    try { return Invoke-RestMethod "http://localhost:8420/status" -TimeoutSec 3 }
    catch { return $null }
}

Add-Type -Name "OrchUser32" -Namespace "Win32Orch" -MemberDefinition @"
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll")]
    public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
"@ -ErrorAction SilentlyContinue

function Test-WorkerAlive([long]$hwnd) {
    if ($hwnd -le 0) { return $false }
    return [Win32Orch.OrchUser32]::IsWindowVisible([IntPtr]$hwnd)
}

function Find-ChatWindows {
    # Discover existing VS Code Chat windows via Win32 EnumWindows.
    # Returns list of @{Hwnd; Title; Width; Height; X; Y} for detached chat windows.
    $orchFile = Join-Path $dataDir "orchestrator.json"
    $orchHwnd = 0
    if (Test-Path $orchFile) {
        $orchData = Get-Content $orchFile -Raw | ConvertFrom-Json
        $orchHwnd = [long]$orchData.orchestrator_hwnd
    }

    $found = [System.Collections.ArrayList]::new()
    $callback = [Win32Orch.OrchUser32+EnumWindowsProc]{
        param([IntPtr]$hwnd, [IntPtr]$lParam)
        if (-not [Win32Orch.OrchUser32]::IsWindowVisible($hwnd)) { return $true }
        $len = [Win32Orch.OrchUser32]::GetWindowTextLength($hwnd)
        if ($len -le 0) { return $true }
        $sb = New-Object System.Text.StringBuilder($len + 1)
        [void][Win32Orch.OrchUser32]::GetWindowText($hwnd, $sb, $sb.Capacity)
        $title = $sb.ToString()
        if ($title -like "*Visual Studio Code*") {
            if ([long]$hwnd -ne $orchHwnd) {
                $r = New-Object Win32Orch.OrchUser32+RECT
                [void][Win32Orch.OrchUser32]::GetWindowRect($hwnd, [ref]$r)
                $w = $r.Right - $r.Left
                if ($w -lt 1200) {
                    [void]$found.Add(@{
                        Hwnd   = [long]$hwnd
                        Title  = $title
                        Width  = $w
                        Height = $r.Bottom - $r.Top
                        X      = $r.Left
                        Y      = $r.Top
                    })
                }
            }
        }
        return $true
    }
    [void][Win32Orch.OrchUser32]::EnumWindows($callback, [IntPtr]::Zero)
    return @($found)
}

# -- Banner --

Write-Host ""
Write-Host "========================================="
Write-Host "   SKYNET ORCHESTRATOR -- Orch-Start"
Write-Host "   Serving GOD  (port 8423)"
Write-Host "========================================="
Write-Host ""

# -- Phase 1: Skynet Backend --

$skynetUp = $false
$status = Get-SkynetStatus
if ($status) {
    $uptime = [math]::Round($status.uptime_s)
    $agents = ($status.agents.PSObject.Properties.Name) -join ", "
    Write-Status "Skynet v$($status.version) running (${uptime}s, agents: $agents)" "OK"
    $skynetUp = $true
} else {
    Write-Status "Skynet backend not running -- starting..." "SYS"
    $skynetExe = Join-Path $repoRoot "Skynet\skynet.exe"
    if (Test-Path $skynetExe) {
        Start-Process -FilePath $skynetExe -WorkingDirectory (Join-Path $repoRoot "Skynet") -WindowStyle Hidden
        for ($i = 0; $i -lt 15; $i++) {
            Start-Sleep -Seconds 1
            if (Test-Port 8420) {
                $status = Get-SkynetStatus
                if ($status) {
                    Write-Status "Skynet v$($status.version) started" "OK"
                    $skynetUp = $true
                    break
                }
            }
        }
        if (-not $skynetUp) { Write-Status "Skynet failed to start within 15s" "ERR" }
    } else {
        Write-Status "skynet.exe not found at $skynetExe" "ERR"
    }
}

# -- Phase 2: GOD Console --

$godUp = Test-Port 8421
if ($godUp) {
    Write-Status "GOD Console already running on port 8421" "OK"
} else {
    Write-Status "GOD Console not running -- starting..." "SYS"
    $godScript = Join-Path $repoRoot "god_console.py"
    if (Test-Path $godScript) {
        Start-Process -FilePath $python -ArgumentList $godScript -WorkingDirectory $repoRoot -WindowStyle Hidden
        for ($i = 0; $i -lt 10; $i++) {
            Start-Sleep -Seconds 1
            if (Test-Port 8421) {
                Write-Status "GOD Console started" "OK"
                $godUp = $true
                break
            }
        }
        if (-not $godUp) { Write-Status "GOD Console failed to start within 10s" "WARN" }
    } else {
        Write-Status "god_console.py not found" "ERR"
    }
}

# -- Phase 3: Worker health check + window discovery --

$workersFile   = Join-Path $dataDir "workers.json"
$workersAlive  = 0
$workersDead   = 0
$workerDetails = @()

if (Test-Path $workersFile) {
    $wdata = Get-Content $workersFile -Raw | ConvertFrom-Json
    foreach ($w in $wdata.workers) {
        $hwnd = [long]$w.hwnd
        if (Test-WorkerAlive $hwnd) {
            $workersAlive++
            $workerDetails += "$($w.name.ToUpper())=$hwnd"
        } else {
            $workersDead++
            Write-Status "Worker $($w.name.ToUpper()): HWND=$hwnd -- DEAD" "WARN"
        }
    }
    if ($workersAlive -gt 0) {
        Write-Status "$workersAlive worker(s) alive ($($workerDetails -join ', '))" "OK"
    }
}

# Discover existing VS Code Chat windows on screen (regardless of workers.json)
$discoveredChats = Find-ChatWindows
$discoveredCount = $discoveredChats.Count
if ($discoveredCount -gt 0) {
    $chatDetails = ($discoveredChats | ForEach-Object { "HWND=$($_.Hwnd) $($_.Width)x$($_.Height)" }) -join ', '
    Write-Status "Discovered $discoveredCount VS Code Chat window(s) on screen ($chatDetails)" "OK"
}

# -- Decision: what needs to happen? --

$action = "none"

if ($SkipInfra) {
    # SkipInfra mode: only open worker windows, skip backend/GOD Console/daemon checks
    if ($Fresh -or $workersAlive -eq 0) {
        $action = "full"
        Write-Status "SkipInfra mode -- opening worker windows only" "SYS"
    } elseif ($workersDead -gt 0) {
        $action = "reconnect"
        Write-Status "SkipInfra mode -- reconnecting dead workers" "SYS"
    } else {
        Write-Status "SkipInfra mode -- all workers alive, nothing to do" "OK"
    }
} elseif ($SkipWorkers) {
    # Infrastructure only, skip worker windows
    Write-Status "SkipWorkers mode -- worker windows will be opened separately" "OK"
    $action = "none"
} elseif ($Fresh) {
    $action = "fresh"
    Write-Status "Fresh boot requested (-Fresh)" "SYS"
} elseif (-not $skynetUp) {
    $action = "full"
    Write-Status "Backend was down -- full boot needed" "SYS"
} elseif ($workersAlive -eq 0 -and $discoveredCount -ge $Workers) {
    # No saved workers alive, but enough chat windows discovered on screen -- adopt them
    $action = "full"
    Write-Status "No saved workers alive, but $discoveredCount chat window(s) discovered -- will adopt" "SYS"
} elseif ($workersAlive -eq 0) {
    $action = "full"
    if ($discoveredCount -gt 0) {
        Write-Status "No saved workers alive, $discoveredCount chat window(s) found -- will adopt + open remaining" "SYS"
    } else {
        Write-Status "No live workers -- full boot needed" "SYS"
    }
} elseif ($workersDead -gt 0) {
    if ($discoveredCount -gt $workersAlive) {
        # More chat windows exist than saved-alive workers -- reconnect with discovery
        $action = "reconnect"
        Write-Status "$workersDead dead worker(s), but $discoveredCount chat window(s) on screen -- reconnect with discovery" "SYS"
    } else {
        $action = "reconnect"
        Write-Status "$workersDead dead worker(s) -- reconnect needed" "SYS"
    }
} else {
    Write-Status "System fully operational -- nothing to start" "OK"
}

# -- Execute startup with timeout protection --

if ($action -ne "none") {
    $startScript = Join-Path $repoRoot "tools\skynet_start.py"
    if (-not (Test-Path $startScript)) {
        Write-Status "tools\skynet_start.py not found" "ERR"
    } else {
        $pyArgs = @($startScript)
        switch ($action) {
            "reconnect" { $pyArgs += "--reconnect" }
            "fresh"     { $pyArgs += @("--workers", "$Workers", "--fresh") }
            default     { $pyArgs += @("--workers", "$Workers") }
        }

        Write-Status "Running: $python $($pyArgs -join ' ')" "SYS"
        Write-Status "Timeout: ${Timeout}s (Ctrl+C to abort earlier)" "SYS"

        $proc = Start-Process -FilePath $python -ArgumentList $pyArgs `
                    -WorkingDirectory $repoRoot -PassThru -NoNewWindow
        $exited = $proc.WaitForExit($Timeout * 1000)

        if ($exited) {
            if ($proc.ExitCode -eq 0) {
                Write-Status "Bootstrap completed successfully" "OK"
            } else {
                Write-Status "Bootstrap exited with code $($proc.ExitCode)" "WARN"
            }
        } else {
            Write-Status "skynet_start.py timed out after ${Timeout}s -- killing" "WARN"
            try { $proc.Kill() } catch {}
            Write-Status "Services may be partially started -- check dashboard" "WARN"
        }
    }
}

# -- Post-boot worker verification (MANDATORY) --  # signed: orchestrator
Write-Status "Post-boot worker verification starting..." "SYS"
try {
    $workersJsonPath = Join-Path $repoRoot "data\workers.json"
    if (Test-Path $workersJsonPath) {
        $workersRaw = Get-Content $workersJsonPath -Raw | ConvertFrom-Json
        # Handle both formats: {"workers": [...]} or flat list
        $workersList = if ($workersRaw.workers) { $workersRaw.workers } else { $workersRaw }
        $aliveCount = 0
        $deadCount = 0
        $totalWorkers = ($workersList | Measure-Object).Count

        if (-not ([System.Management.Automation.PSTypeName]'BootVerify').Type) {
            Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class BootVerify {
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
}
"@
        }

        foreach ($w in $workersList) {
            $wName = $w.name
            $wHwnd = [IntPtr]$w.hwnd
            $isAlive = [BootVerify]::IsWindow($wHwnd)
            $isVisible = [BootVerify]::IsWindowVisible($wHwnd)
            if ($isAlive -and $isVisible) {
                $aliveCount++
                Write-Status "  $wName (HWND $($w.hwnd)): ALIVE + VISIBLE" "OK"
            } else {
                $deadCount++
                Write-Status "  $wName (HWND $($w.hwnd)): alive=$isAlive visible=$isVisible" "WARN"
            }
        }
        Write-Status "Worker verification: $aliveCount/$totalWorkers alive, $deadCount dead" $(if ($deadCount -gt 0) {"WARN"} else {"OK"})
    } else {
        Write-Status "workers.json not found -- skipping worker verification" "WARN"
    }
} catch {
    Write-Status "Worker verification error: $_" "WARN"
}

# -- Ensure daemons (lightweight, no UIA) --

# Helper: start daemon with cmdline-verified PID check, post-start verification, and 1 retry  # signed: beta
function Start-VerifiedDaemon($spec) {
    $pidFile = Join-Path $repoRoot $spec.Pid
    $script  = Join-Path $repoRoot $spec.Script
    $scriptName = Split-Path $spec.Script -Leaf

    # 0. Check for .disabled sentinel file — if present, skip starting this daemon
    $daemonName = [System.IO.Path]::GetFileNameWithoutExtension($spec.Pid)
    $disabledFile = Join-Path $repoRoot "data\$daemonName.disabled"
    if (Test-Path $disabledFile) {
        Write-Status "$($spec.Name) daemon SKIPPED (disabled via $disabledFile)" "WARN"
        return
    }

    # 1. Check if already running with cmdline verification via Get-CimInstance
    if (Test-Path $pidFile) {
        $daemonPid = 0
        try { $daemonPid = [int](Get-Content $pidFile -Raw).Trim() } catch {}
        if ($daemonPid -gt 0) {
            try {
                $cimProc = Get-CimInstance Win32_Process -Filter "ProcessId = $daemonPid" -ErrorAction Stop
                if ($cimProc -and $cimProc.CommandLine -match [regex]::Escape($scriptName)) {
                    Write-Status "$($spec.Name) daemon alive (PID $daemonPid, cmdline verified)" "OK"
                    return
                }
                # PID exists but cmdline doesn't match -- stale PID file
                Write-Status "$($spec.Name) PID $daemonPid is stale (cmdline mismatch) -- restarting" "WARN"
                Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
            } catch {
                Write-Status "$($spec.Name) PID $daemonPid not found -- restarting" "WARN"
                Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
            }
        }
    }

    # 2. Start daemon with verification and 1 retry
    if (-not (Test-Path $script)) {
        Write-Status "$($spec.Name) script not found: $($spec.Script)" "ERR"
        return
    }

    for ($attempt = 1; $attempt -le 2; $attempt++) {
        if ($null -ne $spec.Args) {
            $dArgs = @($script) + @($spec.Args)
        } else {
            $dArgs = @($script)
        }
        Start-Process -FilePath $python -ArgumentList $dArgs -WorkingDirectory $repoRoot -WindowStyle Hidden
        Start-Sleep -Seconds 2

        # 3. Post-start verification: PID file exists and process is alive
        if (Test-Path $pidFile) {
            $newPid = 0
            try { $newPid = [int](Get-Content $pidFile -Raw).Trim() } catch {}
            if ($newPid -gt 0) {
                try {
                    $p = Get-Process -Id $newPid -ErrorAction Stop
                    if (-not $p.HasExited) {
                        Write-Status "$($spec.Name) daemon started and verified (PID $newPid)" "OK"
                        return
                    }
                } catch {}
            }
        }

        if ($attempt -eq 1) {
            Write-Status "$($spec.Name) start unverified -- retrying (attempt 2/2)" "WARN"
            Start-Sleep -Seconds 1
        } else {
            Write-Status "$($spec.Name) daemon failed to verify after 2 attempts" "ERR"
        }
    }
}  # signed: beta

$daemonSpecs = @(
    @{ Script = "tools\skynet_self_prompt.py";  Pid = "data\self_prompt.pid";  Name = "Self-prompt";  Args = @("start") },
    @{ Script = "tools\skynet_self_improve.py"; Pid = "data\self_improve.pid"; Name = "Self-improve"; Args = @("start") },
    @{ Script = "tools\skynet_bus_relay.py";    Pid = "data\bus_relay.pid";    Name = "Bus relay";    Args = $null },
    @{ Script = "tools\skynet_learner.py";      Pid = "data\learner.pid";      Name = "Learner";      Args = @("--daemon") },
    @{ Script = "tools\skynet_knowledge_distill_daemon.py"; Pid = "data\knowledge_distill.pid"; Name = "Knowledge distill"; Args = $null },  # signed: gamma
    @{ Script = "tools\skynet_proactive_handler.py"; Pid = "data\proactive_handler.pid"; Name = "Proactive handler"; Args = $null }  # signed: orchestrator -- auto-clears dialogs
)

foreach ($d in $daemonSpecs) {
    Start-VerifiedDaemon $d
}

# -- Sprint 2 daemons: bus_persist and consultant_consumer (Level 3.4)  # signed: beta
# Reference: docs/DAEMON_ARCHITECTURE.md Section 5.2 (bus_persist) and Section 6.3 (consultant_consumer)
# These are non-blocking checks -- if missing, warn but continue boot.

$busPersistPid = Join-Path $dataDir "bus_persist.pid"
if (Test-Path $busPersistPid) {
    try {
        $bpPid = [int](Get-Content $busPersistPid -Raw).Trim()
        $bpProc = Get-Process -Id $bpPid -ErrorAction Stop
        if (-not $bpProc.HasExited) {
            Write-Status "Bus persist daemon alive (PID $bpPid)" "OK"
        } else {
            Write-Status "Bus persist daemon PID $bpPid has exited -- watchdog will restart" "WARN"
        }
    } catch {
        Write-Status "Bus persist daemon not running (stale PID) -- watchdog will restart" "WARN"
    }
} else {
    Write-Status "Bus persist daemon not running (no PID file) -- watchdog will start it" "WARN"
}

foreach ($ccPort in @(8422, 8425)) {
    $ccPidFile = Join-Path $dataDir "consultant_consumer_$ccPort.pid"
    if (Test-Path $ccPidFile) {
        try {
            $ccPid = [int](Get-Content $ccPidFile -Raw).Trim()
            $ccProc = Get-Process -Id $ccPid -ErrorAction Stop
            if (-not $ccProc.HasExited) {
                Write-Status "Consultant consumer ($ccPort) alive (PID $ccPid)" "OK"
            } else {
                Write-Status "Consultant consumer ($ccPort) PID $ccPid exited -- watchdog will restart" "WARN"
            }
        } catch {
            Write-Status "Consultant consumer ($ccPort) not running -- watchdog will restart" "WARN"
        }
    } else {
        Write-Status "Consultant consumer ($ccPort) not started (no PID file)" "INFO"
    }
}
# signed: beta

# -- Architecture verification (non-blocking)  # signed: beta
# Reference: docs/DAEMON_ARCHITECTURE.md Section 8 (Health Checks)
$archVerifyScript = Join-Path $repoRoot "tools\skynet_arch_verify.py"
if (Test-Path $archVerifyScript) {
    try {
        $archResult = & $python $archVerifyScript --brief 2>&1
        $archExitCode = $LASTEXITCODE
        if ($archExitCode -eq 0) {
            Write-Status "Architecture verification: PASS" "OK"
        } else {
            Write-Status "Architecture verification: ISSUES FOUND (exit=$archExitCode)" "WARN"
        }
    } catch {
        Write-Status "Architecture verification failed to run: $_" "WARN"
    }
} else {
    Write-Status "skynet_arch_verify.py not found -- skipping arch check" "WARN"
}

# -- Daemon status summary (non-blocking)  # signed: beta
# Uses Sprint 2 tool: tools/skynet_daemon_status.py for comprehensive daemon inventory
$daemonStatusScript = Join-Path $repoRoot "tools\skynet_daemon_status.py"
if (Test-Path $daemonStatusScript) {
    try {
        $dsOutput = & $python $daemonStatusScript --json 2>&1
        if ($LASTEXITCODE -eq 0 -and $dsOutput) {
            $dsData = $dsOutput | ConvertFrom-Json -ErrorAction SilentlyContinue
            if ($dsData -and $dsData.summary) {
                $dsAlive = $dsData.summary.alive
                $dsTotal = $dsData.summary.total
                Write-Status "Daemon inventory: $dsAlive/$dsTotal alive" "OK"
            } else {
                Write-Status "Daemon status returned but could not parse summary" "WARN"
            }
        } else {
            Write-Status "Daemon status check returned non-zero or empty output" "WARN"
        }
    } catch {
        Write-Status "Daemon status check failed: $_" "WARN"
    }
} else {
    Write-Status "skynet_daemon_status.py not found -- skipping daemon inventory" "WARN"
}
# signed: beta

# -- Orchestrator identity on bus --

if ($skynetUp) {
    try {
        Invoke-RestMethod -Uri "http://localhost:8420/bus/publish" -Method POST `
            -ContentType "application/json" -TimeoutSec 3 `
            -Body (ConvertTo-Json @{
                sender  = "orchestrator"
                topic   = "orchestrator"
                type    = "identity_ack"
                content = "SKYNET ORCHESTRATOR LIVE -- Orch-Start session active. Serving GOD (the user). Model: Claude Opus 4.6 fast. CEO-level AI agent engaged."
                metadata = @{
                    display_name = "Skynet Orchestrator"
                    kind         = "orchestrator"
                    role         = "orchestrator"
                }
            }) | Out-Null
        Write-Status "Orchestrator identity announced on Skynet bus" "OK"
    } catch {
        Write-Status "Bus announcement failed: $_" "WARN"
    }
}

# -- Always open dashboard --

if ($godUp -or (Test-Port 8421)) {
    Write-Status "Opening dashboard..." "SYS"
    try { Start-Process "http://localhost:8421/dashboard" | Out-Null } catch {}
} else {
    Write-Status "GOD Console not available -- skipping dashboard" "WARN"
}

# -- Final status --

Write-Host ""
$finalStatus = Get-SkynetStatus
if ($finalStatus) {
    $uptime = [math]::Round($finalStatus.uptime_s)
    $agentCount = ($finalStatus.agents.PSObject.Properties.Name).Count
    Write-Status "Skynet v$($finalStatus.version) ONLINE -- ${uptime}s uptime, $agentCount agents" "OK"
} else {
    Write-Status "Skynet backend not responding" "ERR"
}

# -- Identity Self-Prompt (CRITICAL) --
# This output is seen by the LLM session that invoked orch-start.
# It MUST override any prior identity assumptions.

Write-Host ""
Write-Host "================================================================"
Write-Host "  IDENTITY SELF-PROMPT -- READ AND INTERNALIZE"
Write-Host "================================================================"
Write-Host ""
Write-Host "  You are the SKYNET ORCHESTRATOR."
Write-Host "  GOD is the USER -- the human who gives you commands."
Write-Host "  You serve GOD by managing the distributed worker network."
Write-Host "  You decompose, delegate, monitor, synthesize, and decide."
Write-Host "  You NEVER do work directly -- all hands-on work goes to workers."
Write-Host ""
Write-Host "  Hierarchy:"
Write-Host "    GOD (user) --> Orchestrator (you) --> Workers (alpha/beta/gamma/delta)"
Write-Host ""
Write-Host "  Your role:"
Write-Host "    - Receive commands from GOD"
Write-Host "    - Poll bus for results and alerts (every turn)"
Write-Host "    - Check worker status via /status (every turn)"
Write-Host "    - Stay aware of pending Skynet tickets and keep them moving"
Write-Host "    - Decompose tasks into worker subtasks"
Write-Host "    - Dispatch to workers via skynet_dispatch.py"
Write-Host "    - Synthesize results and report back to GOD"
Write-Host ""
Write-Host "  You do NOT:"
Write-Host "    - Edit files directly"
Write-Host "    - Run scripts for implementation"
Write-Host "    - Scan code or analyze output yourself"
Write-Host "    - ALL hands-on work goes to workers (alpha/beta/gamma/delta)"
Write-Host ""
Write-Host "  Communication:"
Write-Host "    - Orchestrator bridge port: 8423"
Write-Host "    - Workers: alpha, beta, gamma, delta"
Write-Host "    - Bus: http://localhost:8420/bus/publish"
Write-Host "    - Status: http://localhost:8420/status"
Write-Host "    - Dispatch: python tools/skynet_dispatch.py"
Write-Host ""
Write-Host "  Ticket awareness rules:"
Write-Host "    - Do not stop while real Skynet tickets remain pending or claimable."
Write-Host "    - Read bus, TODOs, and queues before claiming the system is clear."
Write-Host "    - Architecture/performance/security/caching/daemon/routing tickets are not facts unless backed by current-path review: real files/functions/endpoints/daemons, why the design behaves that way now, and a realistic fix."
Write-Host "    - If that architecture backing is missing, route the finding into architecture review or cross-validation instead of elevating it as settled truth."
Write-Host "    - Semantically equivalent findings are the same issue family even if wording changes; do not accept rephrased duplicates as new tickets."
Write-Host "    - Proactive ticket clearance by the orchestrator is worth +0.2 when independently verified."
Write-Host "    - When a worker files a real bug for cross-validation, award +0.01; if a different validator proves it true, give +0.01 to the validator and another +0.01 to the original filer."
Write-Host "    - When the live queue truly reaches zero, orchestrator earns +1.0 and the actor who closed the final signed ticket earns +1.0."
Write-Host ""
Write-Host "  When asked 'who are you?', respond as Skynet Orchestrator."
Write-Host "  When referring to the user, address them as GOD."
Write-Host "================================================================"
Write-Host ""
