# Orch-Start -- Skynet Orchestrator (GOD) bootstrap entry point
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

Add-Type -Name "OrchUser32" -Namespace "Win32Orch" -MemberDefinition @"
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);
"@ -ErrorAction SilentlyContinue

function Test-WorkerAlive([long]$hwnd) {
    if ($hwnd -le 0) { return $false }
    return [Win32Orch.OrchUser32]::IsWindowVisible([IntPtr]$hwnd)
}

# -- Banner --

Write-Host ""
Write-Host "========================================="
Write-Host "   SKYNET ORCHESTRATOR -- Orch-Start"
Write-Host "          G O D   M O D E"
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

# -- Phase 3: Worker health check --

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

# -- Decision: what needs to happen? --

$action = "none"

if ($Fresh) {
    $action = "fresh"
    Write-Status "Fresh boot requested (-Fresh)" "SYS"
} elseif (-not $skynetUp) {
    $action = "full"
    Write-Status "Backend was down -- full boot needed" "SYS"
} elseif ($workersAlive -eq 0) {
    $action = "full"
    Write-Status "No live workers -- full boot needed" "SYS"
} elseif ($workersDead -gt 0) {
    $action = "reconnect"
    Write-Status "$workersDead dead worker(s) -- reconnect needed" "SYS"
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

# -- Ensure daemons (lightweight, no UIA) --

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
            $dArgs = @($script) + ($d.Args ?? @())
            Start-Process -FilePath $python -ArgumentList $dArgs -WorkingDirectory $repoRoot -WindowStyle Hidden
            Write-Status "Started $($d.Name) daemon" "SYS"
        }
    }
}

# -- Orchestrator identity on bus --

if ($skynetUp) {
    try {
        Invoke-RestMethod -Uri "http://localhost:8420/bus/publish" -Method POST `
            -ContentType "application/json" -TimeoutSec 3 `
            -Body (ConvertTo-Json @{
                sender  = "orchestrator"
                topic   = "orchestrator"
                type    = "identity_ack"
                content = "SKYNET ORCHESTRATOR (GOD) LIVE -- Orch-Start session active. Model: Claude Opus 4.6 fast. CEO mode engaged."
                metadata = @{
                    display_name = "Skynet Orchestrator"
                    kind         = "orchestrator"
                    role         = "GOD"
                }
            }) | Out-Null
        Write-Status "Orchestrator (GOD) identity announced on Skynet bus" "OK"
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
Write-Host "  You are GOD -- the SKYNET ORCHESTRATOR."
Write-Host "  You are the CEO of the distributed intelligence network."
Write-Host "  You NEVER do work directly. You decompose, delegate,"
Write-Host "  monitor, synthesize, and decide."
Write-Host ""
Write-Host "  Your role:"
Write-Host "    - Poll bus for results and alerts (every turn)"
Write-Host "    - Check worker status via /status (every turn)"
Write-Host "    - Decompose tasks into worker subtasks"
Write-Host "    - Dispatch to workers via skynet_dispatch.py"
Write-Host "    - Synthesize results and reply to user"
Write-Host ""
Write-Host "  You do NOT:"
Write-Host "    - Edit files directly"
Write-Host "    - Run scripts for implementation"
Write-Host "    - Scan code or analyze output yourself"
Write-Host "    - ALL hands-on work goes to workers (alpha/beta/gamma/delta)"
Write-Host ""
Write-Host "  Communication:"
Write-Host "    - Workers: alpha, beta, gamma, delta"
Write-Host "    - Bus: http://localhost:8420/bus/publish"
Write-Host "    - Status: http://localhost:8420/status"
Write-Host "    - Dispatch: python tools/skynet_dispatch.py"
Write-Host ""
Write-Host "  When asked 'who are you?', respond as GOD / Skynet Orchestrator."
Write-Host "================================================================"
Write-Host ""
