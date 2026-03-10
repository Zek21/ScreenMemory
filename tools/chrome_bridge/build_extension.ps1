Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourceDir = Join-Path $root "extension"
$manifestPath = Join-Path $sourceDir "manifest.json"
$manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
$version = $manifest.version

$distDir = Join-Path $root "dist"
New-Item -ItemType Directory -Path $distDir -Force | Out-Null

$zipPath = Join-Path $distDir ("chrome-bridge-extension-{0}.zip" -f $version)
$crxPath = Join-Path $distDir ("chrome-bridge-extension-{0}.crx" -f $version)
$pemPath = Join-Path $distDir ("chrome-bridge-extension-{0}.pem" -f $version)

Remove-Item $zipPath, $crxPath, $pemPath -Force -ErrorAction SilentlyContinue

$stageRoot = Join-Path $env:TEMP ("chrome-bridge-build-{0}-{1}" -f $version, [guid]::NewGuid().ToString("N"))
$zipStage = Join-Path $stageRoot "zip"
$packStage = Join-Path $stageRoot "pack"
$zipSource = Join-Path $zipStage "chrome-bridge"
$packSource = Join-Path $packStage "chrome-bridge"

New-Item -ItemType Directory -Path $zipStage, $packStage -Force | Out-Null
Copy-Item $sourceDir $zipSource -Recurse -Force
Copy-Item $sourceDir $packSource -Recurse -Force

Compress-Archive -Path (Join-Path $zipSource "*") -DestinationPath $zipPath -Force

$browserCandidates = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "$env:ProgramFiles(x86)\Google\Chrome\Application\chrome.exe",
  "$env:LocalAppData\Google\Chrome\Application\chrome.exe",
  "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
  "$env:ProgramFiles(x86)\Microsoft\Edge\Application\msedge.exe",
  "$env:LocalAppData\Microsoft\Edge\Application\msedge.exe"
)

$browserPath = $browserCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($browserPath) {
  $arguments = @("--pack-extension=$packSource")
  $process = Start-Process -FilePath $browserPath -ArgumentList $arguments -PassThru -Wait -WindowStyle Hidden

  $generatedCrx = Join-Path $packStage "chrome-bridge.crx"
  $generatedPem = Join-Path $packStage "chrome-bridge.pem"

  if (Test-Path $generatedCrx) {
    Move-Item $generatedCrx $crxPath -Force
  }
  if (Test-Path $generatedPem) {
    Move-Item $generatedPem $pemPath -Force
  }
}

$installNotes = @"
Chrome Bridge $version

Artifacts:
- $zipPath
- $crxPath
- $pemPath

Install:
1. Reliable path: open chrome://extensions, enable Developer mode, click Load unpacked, and select the extension folder.
2. Optional packed file: drag the .crx onto chrome://extensions and test whether your Chrome build accepts it.
3. Real install proof: run `python prove_crx_install.py` from the chrome-bridge folder.

Notes:
- This build proves package creation, not Chrome acceptance.
- Modern Chrome on Windows/macOS often blocks direct install of self-hosted .crx packages.
- A Chrome policy rejection is still a valid proof outcome when captured by prove_crx_install.py.
- Keep the .pem file if you want future packed builds to keep the same extension ID.
"@

$notesPath = Join-Path $distDir "INSTALL.txt"
Set-Content -Path $notesPath -Value $installNotes -NoNewline

Remove-Item $stageRoot -Recurse -Force -ErrorAction SilentlyContinue

Write-Output ("ZIP: {0}" -f $zipPath)
if (Test-Path $crxPath) {
  Write-Output ("CRX: {0}" -f $crxPath)
}
if (Test-Path $pemPath) {
  Write-Output ("PEM: {0}" -f $pemPath)
}
Write-Output ("NOTES: {0}" -f $notesPath)
Write-Output "NEXT: cd $root && python prove_crx_install.py"
