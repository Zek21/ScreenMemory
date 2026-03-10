Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$extensionDir = Join-Path $root "extension"

if (-not (Test-Path $extensionDir)) {
  Write-Error "Extension directory not found: $extensionDir"
  exit 1
}

$chromeUserData = Join-Path $env:LocalAppData "Google\Chrome\User Data"
if (-not (Test-Path $chromeUserData)) {
  Write-Error "Chrome User Data not found: $chromeUserData"
  exit 1
}

# Find Chrome executable
$browserCandidates = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "$env:ProgramFiles(x86)\Google\Chrome\Application\chrome.exe",
  "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
)
$chromePath = $browserCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chromePath) {
  Write-Error "Chrome not found"
  exit 1
}

Write-Output "Chrome Bridge v4.0 - Multi-Profile Installer"
Write-Output "============================================="
Write-Output "Chrome:    $chromePath"
Write-Output "Extension: $extensionDir"
Write-Output ""

# Discover profiles
$profiles = Get-ChildItem $chromeUserData -Directory |
  Where-Object { $_.Name -match '^(Default|Profile \d+)$' } |
  Sort-Object Name

Write-Output "Found $($profiles.Count) Chrome profiles:"
foreach ($prof in $profiles) {
  # Try to read profile name from Preferences
  $prefsPath = Join-Path $prof.FullName "Preferences"
  $profileName = $prof.Name
  if (Test-Path $prefsPath) {
    try {
      $prefs = Get-Content $prefsPath -Raw | ConvertFrom-Json
      if ($prefs.profile.name) {
        $profileName = "$($prof.Name) ($($prefs.profile.name))"
      }
    } catch {}
  }
  Write-Output "  - $profileName"
}
Write-Output ""

# Check if Chrome is running
$chromeProcs = Get-Process -Name "chrome" -ErrorAction SilentlyContinue
if ($chromeProcs) {
  Write-Output "WARNING: Chrome is currently running with $($chromeProcs.Count) processes."
  Write-Output "The extension will be loaded when each profile is launched."
  Write-Output ""
}

# Install to each profile by launching Chrome with --load-extension for each profile
$extPath = (Resolve-Path $extensionDir).Path
$installed = 0

foreach ($prof in $profiles) {
  $profileDir = $prof.Name
  $prefsPath = Join-Path $prof.FullName "Preferences"
  $profileName = $profileDir

  if (Test-Path $prefsPath) {
    try {
      $prefs = Get-Content $prefsPath -Raw | ConvertFrom-Json
      if ($prefs.profile.name) { $profileName = "$profileDir ($($prefs.profile.name))" }
    } catch {}
  }

  Write-Output "Installing to $profileName..."

  # Launch Chrome with the profile and load the extension
  $arguments = @(
    "--profile-directory=`"$profileDir`"",
    "--load-extension=`"$extPath`"",
    "--no-first-run",
    "--disable-default-apps",
    "chrome://extensions"
  )

  $process = Start-Process -FilePath $chromePath -ArgumentList $arguments -PassThru
  # Wait a moment for Chrome to register the extension, then let it stay open
  Start-Sleep -Seconds 3
  $installed++
  Write-Output "  Launched Chrome for $profileName (PID: $($process.Id))"
}

Write-Output ""
Write-Output "============================================="
Write-Output "Done! Launched Chrome for $installed profiles."
Write-Output ""
Write-Output "Each Chrome window opened to chrome://extensions."
Write-Output "The extension should appear as 'Chrome Bridge' in each profile."
Write-Output ""
Write-Output "If any profile shows the extension as disabled:"
Write-Output "  1. Enable Developer Mode (toggle in top-right)"
Write-Output "  2. The extension will activate automatically"
Write-Output ""
Write-Output "The extension persists across browser restarts for each profile"
Write-Output "that has Developer Mode enabled."
