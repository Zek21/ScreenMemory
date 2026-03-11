param(
    [switch]$Fresh,
    [int]$Workers = 4
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$startScript = Join-Path $repoRoot "tools\skynet_start.py"
if (-not (Test-Path $startScript)) {
    throw "tools\skynet_start.py not found under $repoRoot"
}

$venvPython = Join-Path (Split-Path $repoRoot -Parent) "env\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }

$useReconnect = $false
if (-not $Fresh) {
    try {
        $null = Invoke-RestMethod "http://localhost:8420/status" -TimeoutSec 2
        if (Test-Path (Join-Path $repoRoot "data\workers.json")) {
            $useReconnect = $true
        }
    }
    catch {
        $useReconnect = $false
    }
}

$args = @($startScript)
if ($useReconnect) {
    $args += "--reconnect"
}
else {
    $args += @("--workers", "$Workers")
    if ($Fresh) {
        $args += "--fresh"
    }
}

& $python @args

try {
    Start-Process "http://localhost:8421/dashboard" | Out-Null
}
catch {
}
