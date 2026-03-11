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
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $ar  = $tcp.BeginConnect("127.0.0.1", $port, $null, $null)
        $ok  = $ar.AsyncWaitHandle.WaitOne($timeoutMs, $false)
        if ($ok) { try { $tcp.EndConnect($ar) } catch {} }
        $tcp.Close()
        return $ok
    } catch { return $false }
}

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

Add-Type -Name "GCUser32" -Namespace "Win32GC" -MemberDefinition @"
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);
"@ -ErrorAction SilentlyContinue

function Test-WorkerAlive([long]$hwnd) {
    if ($hwnd -le 0) { return $false }
    return [Win32GC.GCUser32]::IsWindowVisible([IntPtr]$hwnd)
}

# ── Banner ───────────────────────────────────────────────

Write-Host ""
Write-Host "========================================="
Write-Host "     GEMINI CONSULTANT -- GC-Start"
Write-Host "   Skynet Advisory Peer (port 8425)"
Write-Host "========================================="
Write-Host ""

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

$daemonSpecs = @(
    @{ Script = "tools\skynet_self_prompt.py";  Pid = "data\self_prompt.pid";  Name = "Self-prompt";  Args = @("start") },
    @{ Script = "tools\skynet_self_improve.py"; Pid = "data\self_improve.pid"; Name = "Self-improve"; Args = @("start") },
    @{ Script = "tools\skynet_bus_relay.py";    Pid = "data\bus_relay.pid";    Name = "Bus relay";    Args = $null },
    @{ Script = "tools\skynet_learner.py";      Pid = "data\learner.pid";      Name = "Learner";      Args = @("--daemon") }
)

foreach ($d in $daemonSpecs) {
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
if ((Test-Port 8425 1000) -and (Test-JsonHealth $consultantBridgeHealth 5)) {
    Write-Status "Gemini Consultant bridge already running on port 8425" "OK"
    $consultantBridgeUp = $true
} else {
    Write-Status "Gemini Consultant bridge not running -- starting..." "SYS"
    $bridgeScript = Join-Path $repoRoot "tools\skynet_consultant_bridge.py"
    if (Test-Path $bridgeScript) {
        $bridgeArgs = @(
            $bridgeScript,
            "--id", "gemini_consultant",
            "--display-name", "`"Gemini Consultant`"",
            "--model", "`"Gemini 3.1 Pro (Preview)`"",
            "--source", "GC-Start",
            "--api-port", "8425"
        ) -join " "
        Start-Process -FilePath $python `
            -ArgumentList $bridgeArgs `
            -WorkingDirectory $repoRoot -WindowStyle Hidden
        if (Wait-HealthyEndpoint 8425 $consultantBridgeHealth 40) {
            $consultantBridgeUp = $true
            Write-Status "Gemini Consultant bridge started and passed /health on port 8425" "OK"
        } else {
            Write-Status "Gemini Consultant bridge failed health verification within 40s" "WARN"
        }
    } else {
        Write-Status "tools\\skynet_consultant_bridge.py not found" "ERR"
    }
}

# Announce Gemini Consultant identity on bus
if ($skynetUp) {
    try {
        $identityContent = if ($consultantBridgeUp) {
            "GEMINI CONSULTANT LIVE -- GC-Start session active. Model: Gemini 3.1 Pro (Preview). Advisory peer ready for tasking."
        } else {
            "GEMINI CONSULTANT SESSION ACTIVE -- bridge offline on port 8425. Model: Gemini 3.1 Pro (Preview). Advisory peer not promptable yet."
        }
        $promptTransport = if ($consultantBridgeUp) { "bridge_queue" } else { "unavailable" }
        $routable = if ($consultantBridgeUp) { "true" } else { "false" }
        Invoke-RestMethod -Uri "http://localhost:8420/bus/publish" -Method POST `
            -ContentType "application/json" -TimeoutSec 3 `
            -Body (ConvertTo-Json @{
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
                }
            }) | Out-Null
        Write-Status "Gemini Consultant identity announced on Skynet bus" "OK"
    } catch {
        Write-Status "Bus announcement failed: $_" "WARN"
    }
}

# ── Always open dashboard ────────────────────────────────

if ($godUp -or (Test-Port 8421)) {
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
Write-Host "    - Bus topic for receiving prompts: consultant"
Write-Host "    - Bridge port: 8425"
Write-Host "    - Post results to bus with sender=gemini_consultant"
Write-Host ""
Write-Host "  Failure corrections you must obey:"
Write-Host "    - GC-Start always means Gemini Consultant, never orchestrator."
Write-Host "    - Report model truth as Gemini 3.1 Pro (Preview)."
Write-Host "    - Bring up bridge 8425 before claiming LIVE or routable transport."
Write-Host "    - Bridge truth rule: do not claim LIVE/routable from a transient port-open alone; require a successful /health check and a surviving state heartbeat."
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
Write-Host "    - If you fail or drift: write an artifact, post it to Skynet, and verify delivery."
Write-Host "    - Keep bus payloads schema-safe unless endpoint support is verified."
Write-Host "    - Do not claim success without a live endpoint check or sender-filtered bus confirmation."
Write-Host ""
Write-Host "  When asked 'who are you?', respond as Gemini Consultant."
Write-Host "================================================================"
Write-Host ""
