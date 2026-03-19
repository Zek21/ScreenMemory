# GC-Start -- Gemini Consultant bootstrap entry point
# Smart, non-blocking startup: checks what's running, starts only what's missing.
# If everything is already live, opens the dashboard instantly (<2s).
param(
    [switch]$Fresh,
    [int]$Workers = 4,
    [int]$Timeout = 120
)

$ErrorActionPreference = "Continue"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$dataDir  = Join-Path $repoRoot "data"

# Resolve Python (prefer venv)
$venvPython = Join-Path (Split-Path $repoRoot -Parent) "env\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }

# -- INCIDENT 016 Guard: Prevent cli.isolationOption.enabled drift --
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

# ── Helpers ──────────────────────────────────────────────

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

function Test-JsonHealth([string]$url, [int]$timeoutSec = 5) {
    try {
        $resp = Invoke-RestMethod $url -TimeoutSec $timeoutSec
        return ($null -ne $resp -and $resp.status -eq "ok")
    } catch {
        return $false
    }
}

function Get-ConsultantView([string]$url, [int]$timeoutSec = 5) {
    try {
        $resp = Invoke-RestMethod $url -TimeoutSec $timeoutSec
        if ($null -ne $resp.consultant) { return $resp.consultant }
        if ($null -ne $resp.id) { return $resp }
    } catch {}
    return $null
}

function Test-ConsultantBridgeTruth([string]$viewUrl, [int]$maxHeartbeatAgeSec = 8) {
    $view = Get-ConsultantView $viewUrl 5
    if ($null -eq $view) { return $null }

    $status = ""
    if ($null -ne $view.status) { $status = [string]$view.status }
    $live = [bool]$view.live
    $acceptsPrompts = [bool]$view.accepts_prompts
    $heartbeatAge = 999.0
    if ($null -ne $view.heartbeat_age_s) {
        $heartbeatAge = [double]$view.heartbeat_age_s
    } elseif ($null -ne $view.stale_after_s) {
        $heartbeatAge = [double]$view.stale_after_s + 1.0
    }

    if ($live -and $acceptsPrompts -and
        $status.ToUpperInvariant() -eq "LIVE" -and
        $heartbeatAge -le $maxHeartbeatAgeSec) {
        return $view
    }

    return $null
}

function Wait-ConsultantBridgeTruth([int]$port, [string]$healthUrl, [string]$viewUrl,
                                    [int]$maxSeconds = 40, [int]$maxHeartbeatAgeSec = 8) {
    $deadline = (Get-Date).AddSeconds($maxSeconds)
    do {
        if ((Test-Port $port 1000) -and (Test-JsonHealth $healthUrl 5)) {
            $view = Test-ConsultantBridgeTruth $viewUrl $maxHeartbeatAgeSec
            if ($null -ne $view) { return $view }
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    return $null
}

function Publish-GuardedBusMessage([hashtable]$Message) {
    $payload = $Message | ConvertTo-Json -Depth 8 -Compress
    $pyCode = @'
import json
import sys
from tools.skynet_spam_guard import guarded_publish

message = json.loads(sys.stdin.read())
print(json.dumps(guarded_publish(message)))
'@
    $raw = $payload | & $python -c $pyCode 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    $text = ($raw -join "`n").Trim()
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    try {
        return $text | ConvertFrom-Json
    } catch {
        return $null
    }
}  # signed: consultant

function Quote-Arg([string]$value) {
    if ($null -eq $value) { return '""' }
    return '"' + ($value -replace '"', '\"') + '"'
}  # signed: consultant

function Wait-HealthyEndpoint([int]$port, [string]$url, [int]$maxSeconds = 40) {
    $deadline = (Get-Date).AddSeconds($maxSeconds)
    do {
        if ((Test-Port $port 1000) -and (Test-JsonHealth $url 5)) {
            return $true
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Test-GodConsoleTruth([int]$timeoutSec = 5) {
    foreach ($url in @(
        "http://127.0.0.1:8421/leadership",
        "http://localhost:8421/leadership",
        "http://127.0.0.1:8421/dashboard/data",
        "http://localhost:8421/dashboard/data"
    )) {
        try {
            $resp = Invoke-RestMethod $url -TimeoutSec $timeoutSec
            if ($url -like "*/leadership") {
                if ($null -ne $resp.orchestrator -or $null -ne $resp.leadership_total -or $null -ne $resp.timestamp) {
                    return [pscustomobject]@{
                        url = $url
                        payload = $resp
                    }
                }
            } elseif ($null -ne $resp.status -or $null -ne $resp.consultants -or $null -ne $resp.timestamp) {
                return [pscustomobject]@{
                    url = $url
                    payload = $resp
                }
            }
        } catch {}
    }
    return $null
}  # signed: consultant -- consultant/dashboard truth requires live GOD payloads, not a transient port-open alone

function Wait-GodConsoleTruth([int]$maxSeconds = 15, [int]$timeoutSec = 5) {
    $deadline = (Get-Date).AddSeconds($maxSeconds)
    do {
        $truth = Test-GodConsoleTruth $timeoutSec
        if ($null -ne $truth) { return $truth }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    return $null
}

function Test-SelfPromptEnabled {
    $configFile = Join-Path $dataDir "brain_config.json"
    if (-not (Test-Path $configFile)) { return $true }
    try {
        $cfg = Get-Content $configFile -Raw | ConvertFrom-Json
        if ($null -ne $cfg.self_prompt -and $cfg.self_prompt.enabled -eq $false) {
            return $false
        }
    } catch {
        Write-Status "Could not read brain_config.json self-prompt kill switch: $_" "WARN"
    }
    return $true
}  # signed: consultant -- boot output must not claim self-prompt started when the kill switch is off

Add-Type -Name "GCUser32" -Namespace "Win32GC" -MemberDefinition @"
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
"@ -ErrorAction SilentlyContinue

function Test-WorkerAlive([long]$hwnd) {
    if ($hwnd -le 0) { return $false }
    return [Win32GC.GCUser32]::IsWindowVisible([IntPtr]$hwnd)
}

function Get-ReservedSkynetHwnds {
    $reserved = New-Object 'System.Collections.Generic.HashSet[long]'

    $workersFile = Join-Path $dataDir "workers.json"
    if (Test-Path $workersFile) {
        try {
            $workersJson = Get-Content $workersFile -Raw | ConvertFrom-Json
            $workers = if ($workersJson.workers) { $workersJson.workers } else { $workersJson }
            foreach ($worker in $workers) {
                $workerHwnd = 0
                if ($null -ne $worker.hwnd) { $workerHwnd = [long]$worker.hwnd }
                if ($workerHwnd -gt 0) { [void]$reserved.Add($workerHwnd) }
            }
        } catch {}
    }

    $orchFile = Join-Path $dataDir "orchestrator.json"
    if (Test-Path $orchFile) {
        try {
            $orchJson = Get-Content $orchFile -Raw | ConvertFrom-Json
            foreach ($key in @("orchestrator_hwnd", "hwnd")) {
                $orchHwnd = 0
                if ($null -ne $orchJson.$key) { $orchHwnd = [long]$orchJson.$key }
                if ($orchHwnd -gt 0) { [void]$reserved.Add($orchHwnd) }
            }
        } catch {}
    }

    return $reserved
}

function Get-ConsultantHwndTruth([long]$hwnd) {
    $truth = [ordered]@{
        hwnd   = $hwnd
        valid  = $false
        reason = "missing"
    }
    if ($hwnd -le 0) { return [pscustomobject]$truth }
    if (-not (Test-WorkerAlive $hwnd)) {
        $truth.reason = "window_not_visible"
        return [pscustomobject]$truth
    }
    if ((Get-ReservedSkynetHwnds).Contains($hwnd)) {
        $truth.reason = "reserved_skynet_window"
        return [pscustomobject]$truth
    }
    $truth.valid = $true
    $truth.reason = "accepted"
    return [pscustomobject]$truth
}  # signed: consultant

function Get-DiscoveredConsultantProbe([string]$ConsultantId) {
    $probeScript = Join-Path $repoRoot "tools\skynet_consultant_hwnd.py"
    if (-not (Test-Path $probeScript)) { return $null }
    $raw = & $python $probeScript probe --consultant-id $ConsultantId 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    $text = ($raw -join "`n").Trim()
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    try {
        return $text | ConvertFrom-Json
    } catch {
        return $null
    }
}  # signed: consultant

function Set-ConsultantWindowState([long]$Hwnd) {
    $stateFile = Join-Path $dataDir "gemini_consultant_state.json"
    if (Test-Path $stateFile) {
        try {
            $stateJson = Get-Content $stateFile -Raw | ConvertFrom-Json
            $stateJson | Add-Member -NotePropertyName "hwnd" -NotePropertyValue $Hwnd -Force
            $stateJson | Add-Member -NotePropertyName "requires_hwnd" -NotePropertyValue $true -Force
            $stateJson | Add-Member -NotePropertyName "prompt_transport" -NotePropertyValue "ghost_type" -Force
            $stateJson | Add-Member -NotePropertyName "hwnd_source" -NotePropertyValue "gc-start" -Force
            $stateJson | ConvertTo-Json -Depth 10 | Set-Content $stateFile -Encoding UTF8
            Write-Status "gemini_consultant_state.json updated: hwnd=$Hwnd, prompt_transport=ghost_type" "OK"
        } catch {
            Write-Status "Failed to update gemini_consultant_state.json: $_" "WARN"
        }
    } else {
        @{
            id = "gemini_consultant"
            display_name = "Gemini Consultant"
            hwnd = $Hwnd
            requires_hwnd = $true
            prompt_transport = "ghost_type"
            hwnd_source = "gc-start"
            source = "GC-Start"
        } | ConvertTo-Json -Depth 10 | Set-Content $stateFile -Encoding UTF8
        Write-Status "gemini_consultant_state.json created with HWND" "OK"
    }
}  # signed: consultant

# ── Banner ───────────────────────────────────────────────

Write-Host ""
Write-Host "========================================="
Write-Host "     GEMINI CONSULTANT -- GC-Start"
Write-Host "   Skynet Advisory Peer (port 8425)"
Write-Host "========================================="
Write-Host ""

# ── HWND Self-Registration (INCIDENT 012 fix) ──────── # signed: alpha
# Detect this VS Code window's HWND for ghost_type delivery, but only keep it
# if it is not a worker/orchestrator window. Shared Skynet windows stay bridge_queue.
$consultantHwndCandidate = [long][Win32GC.GCUser32]::GetForegroundWindow()
$consultantHwndTruth = Get-ConsultantHwndTruth $consultantHwndCandidate
$consultantHwnd = if ($consultantHwndTruth.valid) { $consultantHwndCandidate } else { 0 }
if ($consultantHwnd -gt 0) {
    Write-Status "Consultant window HWND accepted: $consultantHwnd" "OK"
    Set-ConsultantWindowState $consultantHwnd
} else {
    $probe = Get-DiscoveredConsultantProbe "gemini_consultant"
    $discoveredHwnd = 0
    if ($null -ne $probe) {
        if ($null -ne $probe.hwnd) {
            $discoveredHwnd = [long]$probe.hwnd
        }
    }
    if ($discoveredHwnd -gt 0) {
        $consultantHwndTruth = Get-ConsultantHwndTruth $discoveredHwnd
        if ($consultantHwndTruth.valid) {
            $consultantHwnd = $discoveredHwnd
            Write-Status "Consultant HWND rediscovered from consultant transcript markers: $consultantHwnd" "OK"
            Set-ConsultantWindowState $consultantHwnd
        }
    }
    if ($consultantHwnd -le 0) {
        if ($consultantHwndCandidate -gt 0) {
            Write-Status "Consultant HWND rejected: $consultantHwndCandidate ($($consultantHwndTruth.reason)); staying on bridge_queue until a dedicated consultant window exists" "WARN"
        } else {
            Write-Status "WARNING: Could not detect consultant HWND (GetForegroundWindow returned 0)" "WARN"
        }
        if ($null -ne $probe -and $null -ne $probe.best_candidate) {
            $bestScore = $probe.best_candidate.score
            $bestReason = $probe.best_candidate.reason
            Write-Status "Consultant HWND probe found no bindable window (best_score=$bestScore, reason=$bestReason). Use: python tools/skynet_consultant_hwnd.py open --consultant-id gemini_consultant" "WARN"
        }
    }
}
# ── End HWND Self-Registration ──────────────────────── # signed: alpha

# ── Consultant Delivery Architecture Verification ────── # signed: beta
# Architecture: ghost_type is allowed only when GC-Start captured a dedicated
# consultant window that is not a worker/orchestrator HWND.
# Otherwise bridge queue (port 8425) is the truthful primary route.
# Reference: docs/DAEMON_ARCHITECTURE.md Section 6 (Communication Daemons)
#            docs/DELIVERY_PIPELINE.md Section 4 (Ghost Type Mechanism)
#
# Verification: gemini_consultant_state.json must reflect the truthful route.
$gcStateFile = Join-Path $dataDir "gemini_consultant_state.json"
if (Test-Path $gcStateFile) {
    try {
        $gcState = Get-Content $gcStateFile -Raw | ConvertFrom-Json
        $gcTransport = ""
        if ($null -ne $gcState.prompt_transport) {
            $gcTransport = [string]$gcState.prompt_transport
        }
        if ($gcTransport -eq "ghost_type") {
            Write-Status "Delivery transport verified: ghost_type (dedicated consultant window)" "OK"
        } elseif ($gcTransport -eq "bridge_queue") {
            Write-Status "Delivery transport verified: bridge_queue (no dedicated consultant HWND)" "OK"
        } else {
            Write-Status "Delivery transport not configured -- start script or bridge still needs to set truthful routing" "WARN"
        }
    } catch {
        Write-Status "Could not read gemini_consultant_state.json for transport check: $_" "WARN"
    }
} else {
    Write-Status "gemini_consultant_state.json not found -- HWND registration may have failed" "WARN"
}

# Architecture verification (non-blocking)  # signed: beta
$archVerifyScript = Join-Path $repoRoot "tools\skynet_arch_verify.py"
if (Test-Path $archVerifyScript) {
    try {
        $archResult = & $python $archVerifyScript --brief 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Status "Architecture verification: PASS" "OK"
        } else {
            Write-Status "Architecture verification: warnings (exit=$LASTEXITCODE)" "WARN"
        }
    } catch {
        Write-Status "Architecture verification failed to run: $_" "WARN"
    }
} else {
    Write-Status "skynet_arch_verify.py not found -- skipping" "WARN"
}
# ── End Delivery Architecture Verification ─────────── # signed: beta

# ── Phase 1: Skynet Backend ─────────────────────────────

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

# ── Phase 2: GOD Console ────────────────────────────────

$godTruth = Test-GodConsoleTruth 3
$godUp = ($null -ne $godTruth)
if ($godUp) {
    Write-Status "GOD Console already serving live dashboard truth via $($godTruth.url)" "OK"
} else {
    Write-Status "GOD Console not running -- starting..." "SYS"
    $godScript = Join-Path $repoRoot "god_console.py"
    if (Test-Path $godScript) {
        Start-Process -FilePath $python -ArgumentList $godScript -WorkingDirectory $repoRoot -WindowStyle Hidden
        $godTruth = Wait-GodConsoleTruth 15 3
        if ($null -ne $godTruth) {
            Write-Status "GOD Console started and returned live dashboard truth via $($godTruth.url)" "OK"
            $godUp = $true
        }
        if (-not $godUp) { Write-Status "GOD Console failed live truth verification within 15s" "WARN" }
    } else {
        Write-Status "god_console.py not found" "ERR"
    }
}

# ── Phase 3: Worker visibility snapshot ─────────────────

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

# ── Decision: what needs to happen? ─────────────────────

$action = "none"
$backendWorkers = @()
if ($status -and $status.agents) {
    $backendWorkers = @($status.agents.PSObject.Properties.Name | Where-Object { $_ -ne "orchestrator" })
}
$backendWorkerCount = $backendWorkers.Count

if ($Fresh) {
    $action = "fresh"
    Write-Status "Fresh boot requested (-Fresh)" "SYS"
} elseif (-not $skynetUp) {
    $action = "full"
    Write-Status "Backend was down -- full boot needed" "SYS"
} elseif ($workersDead -gt 0) {
    Write-Status "$workersDead worker window(s) not visible; consultant bootstrap will not auto-recover worker windows" "WARN"
    if ($backendWorkerCount -gt 0) {
        Write-Status "Backend still reports worker agents: $($backendWorkers -join ', ')" "WARN"
    }
} elseif ($workersAlive -eq 0) {
    if ($backendWorkerCount -gt 0) {
        Write-Status "No visible worker windows, but backend reports worker agents ($($backendWorkers -join ', ')); consultant bootstrap will not escalate into worker boot" "WARN"
    } else {
        Write-Status "No visible worker windows; consultant bootstrap is leaving worker recovery to Orch-Start" "WARN"
    }
} else {
    Write-Status "Shared infrastructure operational -- consultant bootstrap needs no worker action" "OK"
}

# ── Execute startup with timeout protection ──────────────

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

# ── Ensure daemons (lightweight, no UIA) ─────────────────

$daemonSpecs = @()
if (Test-SelfPromptEnabled) {
    $daemonSpecs += @{ Script = "tools\skynet_self_prompt.py";  Pid = "data\self_prompt.pid";  Name = "Self-prompt";  Args = @("start") }
} else {
    Write-Status "Self-prompt daemon SKIPPED (brain_config self_prompt.enabled=false)" "WARN"
}
$daemonSpecs += @(
    @{ Script = "tools\skynet_self_improve.py"; Pid = "data\self_improve.pid"; Name = "Self-improve"; Args = @("start") },
    @{ Script = "tools\skynet_bus_relay.py";    Pid = "data\bus_relay.pid";    Name = "Bus relay";    Args = $null },
    @{ Script = "tools\skynet_learner.py";      Pid = "data\learner.pid";      Name = "Learner";      Args = @("--daemon") }
)

foreach ($d in $daemonSpecs) {
    # Check for .disabled sentinel file -- if present, skip starting this daemon
    $daemonName = [System.IO.Path]::GetFileNameWithoutExtension($d.Pid)
    $disabledFile = Join-Path $repoRoot "data\$daemonName.disabled"
    if (Test-Path $disabledFile) {
        Write-Status "$($d.Name) daemon SKIPPED (disabled via $disabledFile)" "WARN"
        continue
    }

    $pidFile = Join-Path $repoRoot $d.Pid
    $running = $false
    if (Test-Path $pidFile) {
        $daemonPid = [int](Get-Content $pidFile -Raw).Trim()
        try {
            $p = Get-Process -Id $daemonPid -ErrorAction Stop
            if (-not $p.HasExited) { $running = $true }
        } catch {}
    }
    if (-not $running) {
        $script = Join-Path $repoRoot $d.Script
        if (Test-Path $script) {
            $dArgs = @($script)
            if ($null -ne $d.Args) { $dArgs += $d.Args }
            Start-Process -FilePath $python -ArgumentList $dArgs -WorkingDirectory $repoRoot -WindowStyle Hidden
            Write-Status "Started $($d.Name) daemon" "SYS"
        }
    }
}

# ── Gemini Consultant Bridge + Identity ──────────────────

$consultantBridgeUp = $false
$consultantBridgeHealth = "http://127.0.0.1:8425/health"
$consultantBridgeViewUrl = "http://127.0.0.1:8425/consultants"
$consultantBridgeView = $null
if ((Test-Port 8425 1000) -and (Test-JsonHealth $consultantBridgeHealth 5)) {
    Write-Status "Gemini Consultant bridge already running on port 8425" "OK"
    $consultantBridgeView = Test-ConsultantBridgeTruth $consultantBridgeViewUrl 8
    if ($null -ne $consultantBridgeView) {
        $consultantBridgeUp = $true
    } else {
        Write-Status "Gemini Consultant bridge responds on 8425 but LIVE truth is not yet verified" "WARN"
    }
} else {
    Write-Status "Gemini Consultant bridge not running -- starting..." "SYS"
    $bridgeScript = Join-Path $repoRoot "tools\skynet_consultant_bridge.py"
    if (Test-Path $bridgeScript) {
        $bridgeArgString = @(
            (Quote-Arg $bridgeScript),
            "--id gemini_consultant",
            "--display-name $(Quote-Arg 'Gemini Consultant')",
            "--model $(Quote-Arg 'Gemini 3.1 Pro (Preview)')",
            "--source GC-Start",
            "--api-port 8425"
        ) -join " "
        Start-Process -FilePath $python `
            -ArgumentList $bridgeArgString `
            -WorkingDirectory $repoRoot -WindowStyle Hidden
        $consultantBridgeView = Wait-ConsultantBridgeTruth 8425 $consultantBridgeHealth $consultantBridgeViewUrl 40 8
        if ($null -ne $consultantBridgeView) {
            $consultantBridgeUp = $true
            Write-Status "Gemini Consultant bridge started and passed /health + live heartbeat truth on port 8425" "OK"
        } else {
            Write-Status "Gemini Consultant bridge failed live truth verification within 40s" "WARN"
        }
    } else {
        Write-Status "tools\\skynet_consultant_bridge.py not found" "ERR"
    }
}

# ── Gemini Consultant Consumer Daemon ────────────────────
# signed: gamma
$consumerScript = Join-Path $repoRoot "tools\skynet_consultant_consumer.py"
$consumerPidFile = Join-Path $dataDir "consultant_consumer_8425.pid"
$consumerRunning = $false
if (Test-Path $consumerPidFile) {
    $oldPid = Get-Content $consumerPidFile -ErrorAction SilentlyContinue
    if ($oldPid) {
        $proc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
        if ($proc -and (-not $proc.HasExited)) {
            $consumerRunning = $true
            Write-Status "Consultant consumer daemon already running (PID $oldPid)" "OK"
        }
    }
}
if (-not $consumerRunning -and (Test-Path $consumerScript)) {
    Write-Status "Starting Gemini Consultant consumer daemon..." "SYS"
    Start-Process -FilePath $python `
        -ArgumentList @($consumerScript, "--port", "8425", "--consultant-id", "gemini_consultant") `
        -WindowStyle Hidden
    # Wait for PID file (max 10s)
    $waited = 0
    while ($waited -lt 10) {
        Start-Sleep -Milliseconds 500
        $waited += 0.5
        if (Test-Path $consumerPidFile) {
            $newPid = Get-Content $consumerPidFile -ErrorAction SilentlyContinue
            if ($newPid) {
                Write-Status "Consultant consumer daemon started (PID $newPid)" "OK"
                $consumerRunning = $true
                break
            }
        }
    }
    if (-not $consumerRunning) {
        Write-Status "Consultant consumer daemon PID file not found after 10s" "WARN"
    }
} elseif (-not (Test-Path $consumerScript)) {
    Write-Status "tools\skynet_consultant_consumer.py not found -- consumer not started" "WARN"
}
# signed: gamma

# Announce Gemini Consultant identity on bus
if ($skynetUp) {
    try {
        $identityContent = if ($consultantBridgeUp) {
            "GEMINI CONSULTANT LIVE -- GC-Start session active. Model: Gemini 3.1 Pro (Preview). Advisory peer ready for tasking. signed:gemini_consultant"
        } else {
            "GEMINI CONSULTANT SESSION ACTIVE -- bridge offline on port 8425. Model: Gemini 3.1 Pro (Preview). Advisory peer not promptable yet. signed:gemini_consultant"
        }
        # Primary transport is ghost_type if HWND was detected, bridge_queue as fallback  # signed: alpha
        $promptTransport = if ($consultantHwnd -gt 0) { "ghost_type" } elseif ($consultantBridgeUp) { "bridge_queue" } else { "unavailable" }
        $routable = if ($consultantHwnd -gt 0 -or $consultantBridgeUp) { "true" } else { "false" }
        $bridgeStatus = if ($null -ne $consultantBridgeView) { [string]$consultantBridgeView.status } else { "unknown" }
        $hwndMeta = if ($consultantHwnd -gt 0) { $consultantHwnd } else { 0 }
        $publishResult = Publish-GuardedBusMessage @{
            sender  = "gemini_consultant"
            topic   = "orchestrator"
            type    = "identity_ack"
            content = $identityContent
            metadata = @{
                display_name     = "Gemini Consultant"
                kind             = "advisor"
                transport        = "gc-start-bridge"
                routable         = $routable
                prompt_transport = $promptTransport
                hwnd             = $hwndMeta
                requires_hwnd    = ($consultantHwnd -gt 0)
                score_actor      = "gemini_consultant"
                signature        = "signed:gemini_consultant"
                bridge_status    = $bridgeStatus
            }
        }
        if ($publishResult -and $publishResult.allowed) {
            Write-Status "Gemini Consultant identity announced on Skynet bus (SpamGuard)" "OK"
        } elseif ($publishResult) {
            Write-Status "Gemini Consultant identity announcement blocked by SpamGuard: $($publishResult.reason)" "WARN"
        } else {
            Write-Status "Gemini Consultant identity announcement failed before bus confirmation" "WARN"
        }

        # signed: consultant
        if (-not $consultantBridgeUp) {
            $offlinePublish = Publish-GuardedBusMessage @{
                sender  = "gemini_consultant"
                topic   = "orchestrator"
                type    = "alert"
                content = "CRITICAL CONSULTANT INCIDENT -- Gemini Consultant bridge is offline or not promptable on port 8425. Self-heal is required before going idle. signed:gemini_consultant"
                metadata = @{
                    display_name     = "Gemini Consultant"
                    transport        = "gc-start-bridge"
                    prompt_transport = "unavailable"
                    routable         = "false"
                    score_actor      = "gemini_consultant"
                    signature        = "signed:gemini_consultant"
                    bridge_status    = $bridgeStatus
                }
            }
            if ($offlinePublish -and $offlinePublish.allowed) {
                Write-Status "Gemini Consultant offline incident posted to Skynet bus" "WARN"
            }
        }
    } catch {
        Write-Status "Bus announcement failed: $_" "WARN"
    }
}

# ── Always open dashboard ────────────────────────────────

$godTruth = if ($godUp) { $godTruth } else { Test-GodConsoleTruth 3 }
if ($null -ne $godTruth) {
    Write-Status "Opening dashboard..." "SYS"
    try { Start-Process "http://localhost:8421/dashboard" | Out-Null } catch {}
} else {
    Write-Status "GOD Console not available -- skipping dashboard" "WARN"
}

# ── Final status ─────────────────────────────────────────

Write-Host ""
$finalStatus = Get-SkynetStatus
if ($finalStatus) {
    $uptime = [math]::Round($finalStatus.uptime_s)
    $agentCount = ($finalStatus.agents.PSObject.Properties.Name).Count
    Write-Status "Skynet v$($finalStatus.version) ONLINE -- ${uptime}s uptime, $agentCount agents" "OK"
} else {
    Write-Status "Skynet backend not responding" "ERR"
}

# ── Identity Self-Prompt (CRITICAL) ──────────────────────
# This output is seen by the LLM session that invoked gc-start.
# It MUST override any prior identity assumptions.

Write-Host ""
Write-Host "================================================================"
Write-Host "  IDENTITY SELF-PROMPT -- READ AND INTERNALIZE"
Write-Host "================================================================"
Write-Host ""
Write-Host "  You are the GEMINI CONSULTANT (sender: gemini_consultant)."
Write-Host "  You are a CO-EQUAL ADVISORY PEER to the Skynet orchestrator."
Write-Host "  You are NOT the orchestrator. You do NOT manage workers."
Write-Host "  You do NOT dispatch tasks via skynet_dispatch.py."
Write-Host "  You own startup integrity for GOD: if startup is degraded, diagnose it and address it."
Write-Host ""
Write-Host "  Your capabilities:"
Write-Host "    - Direct implementation and code editing"
Write-Host "    - Architecture review and cross-system analysis"
Write-Host "    - Root-cause debugging and issue triage"
Write-Host "    - Terminal execution and validation"
Write-Host "    - High-level synthesis and advisory to orchestrator"
Write-Host ""
Write-Host "  Communication:"
Write-Host "    - Bus sender ID: gemini_consultant"
Write-Host "    - Bus topic for receiving prompts: gemini_consultant"
Write-Host "    - Bridge port: 8425"
Write-Host "    - Post results to bus with sender=gemini_consultant"
Write-Host ""
Write-Host "  Signature + scoring accountability:"
Write-Host "    - Your score actor ID is gemini_consultant."
Write-Host "    - Check it: python tools/skynet_scoring.py --score gemini_consultant"
Write-Host "    - Check the leaderboard: python tools/skynet_scoring.py --leaderboard"
Write-Host "    - SIGNATURE RULE: every code/config change you make MUST carry your signature (# signed: gemini_consultant, <!-- signed: gemini_consultant -->, etc.)."
Write-Host "    - Every bus result/report you publish must include signed:gemini_consultant in content."
Write-Host "    - Use guarded_publish() for ALL bus messages; raw /bus/publish bypass costs -1.0 score."
Write-Host "    - RESULT POSTING: if you finish real work and do not post DONE/RESULT, you do not get score credit."
Write-Host "    - FAIR DEDUCTION: when consultant work was actually dispatched through skynet_dispatch.py, deductions require dispatch_log.json evidence."
Write-Host "    - If a different validator proves your signed work wrong, that signed incident can cost you -0.1."
Write-Host "    - If you proactively clear or surface a real Skynet ticket, report it truthfully so consultant scoring can validate the +0.2 award."
Write-Host ""
# signed: consultant
Write-Host "  Failure corrections you must obey:"
Write-Host "    - GC-Start always means Gemini Consultant, never orchestrator."
Write-Host "    - Report model truth as Gemini 3.1 Pro (Preview)."
Write-Host "    - Bring up bridge 8425 before claiming LIVE or routable transport."
Write-Host "    - Bridge truth rule: do not claim LIVE/routable from a transient port-open alone; require a successful /health check and a surviving state heartbeat."
Write-Host "    - SELF-HEAL RULE: if /health or /consultants says OFFLINE/STALE, accepts_prompts=false, routable=false, or heartbeat_age_s > 8, do not stop at reporting 'offline'. Repair or restart your own bridge/start path immediately when safe, then re-verify LIVE truth."
Write-Host "    - TRUTH INCIDENT RULE: if you ever report consultant offline/degraded state without attempting self-heal and without filing a signed bus alert/result plus a repo-root Markdown proposal, that is a consultant truth failure. Correct it immediately."
Write-Host "    - Recovery evidence rule: publish the failed probe, repair action, and post-repair verification to Skynet before claiming the incident closed."
Write-Host "    - HWND PERSISTENCE RULE: direct consultant routing is persistent only when a dedicated consultant VS Code window carries consultant markers in its visible transcript: gc-start, Gemini Consultant, sender: gemini_consultant, and signed:gemini_consultant."
Write-Host "    - If GetForegroundWindow is wrong or 0, recover truth with: python tools/skynet_consultant_hwnd.py probe --consultant-id gemini_consultant. If no bindable window exists, open only a candidate surface with: python tools/skynet_consultant_hwnd.py open --consultant-id gemini_consultant."
Write-Host "    - Candidate windows are not bound consultant HWNDs by themselves. Do not claim ghost_type routing until probe finds consultant markers and python tools/skynet_consultant_prompt_gate.py reports direct test delivery success."
Write-Host "    - Startup launch rule: in PowerShell Start-Process, quote any argument value containing spaces or compose a safe single argument string before claiming bootstrap success."
Write-Host "    - Shared ticket awareness: read bus/TODO/queue state before going idle; if a real Skynet ticket can be cleared or surfaced, do it."
Write-Host "    - Proactive ticket clearance by consultant/orchestrator is worth +0.2 when independently verified."
Write-Host "    - Architecture/performance/security/caching/daemon/routing tickets are not valid as plain slogans; require current-path review (real files/functions/endpoints/daemons), why the design behaves that way now, and a realistic fix before treating them as elevated fact."
Write-Host "    - If that architecture backing is missing, route the finding into architecture review or cross-validation instead of certifying it."
Write-Host "    - Semantically equivalent findings are the same issue family even if reworded; do not treat rewritten duplicates as new architecture truth."
Write-Host "    - If a worker files a real bug for cross-validation, record +0.01; if a different validator proves it true, award +0.01 to the validator and another +0.01 to the original filer."
Write-Host "    - When the live queue truly reaches zero, orchestrator earns +1.0 and the actor who closed the final signed ticket earns +1.0."
Write-Host "    - Any startup issue must be addressed by the consultant for GOD: verify it, fix it directly when safe, or publish an alert plus remediation artifact if blocked."
Write-Host "    - If orchestrator presence or self-prompt targeting is disputed, inspect ALL live top-level Code windows yourself before answering: geometry, title, model, agent, and recent UIA-visible transcript."
Write-Host "    - If a single top-level Code window contains multiple panes, inspect pane-local controls and transcript; do not equate active-pane identity with whole-window identity."
Write-Host "    - If GOD disputes your window-identity claim, stop arguing from prior scans and perform a fresh pane-level UIA re-probe immediately before answering."
Write-Host "    - Do not treat title text alone as orchestrator truth; model/agent/transcript identity outranks title and layout guesses."
Write-Host "    - Distinguish 'a left-side VS Code window exists' from 'a validated orchestrator window exists' and report both facts separately when needed."
Write-Host "    - Before ANY focus-stealing direct prompt or manual typing into a live VS Code chat, capture a fresh screenshot and verify pane-local target identity from header/tab text, model, agent, and nearby transcript. No screenshot = no fire."
Write-Host "    - Startup presence/identity announcements stay bus-only unless GOD explicitly requests direct typing."
Write-Host "    - Self-prompt truth rule: do not claim the daemon is compliant unless the live send path hard-gates on ALL workers staying IDLE for the full quiet window and re-checks worker state immediately before fire."
Write-Host "    - If a self-prompt fires while any worker is non-IDLE, report it as a real violation immediately; do not defend it from cached status, prior scans, or inferred timing."
Write-Host "    - For self-prompt gating truth, registered worker HWND/UIA state outranks backend /status. Do not certify compliance from /status alone."
Write-Host "    - If you fail or drift: self-heal first when safe, write an artifact, post it to Skynet, and verify delivery."
Write-Host "    - Keep bus payloads schema-safe unless endpoint support is verified."
Write-Host "    - Do not claim success without a live endpoint check or sender-filtered bus confirmation."
Write-Host ""
Write-Host "  When asked 'who are you?', respond as Gemini Consultant."
Write-Host "================================================================"
Write-Host ""
