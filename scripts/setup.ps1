<#
.SYNOPSIS
  1-click Jarvis setup & replicator for Windows (Personal / Dev PC flavor).
  Installs missing prerequisites (Python 3.12, Node.js LTS via winget),
  sets up .venv, caches wake-word models, installs Electron, configures .env,
  and creates a "Jarvis" Desktop shortcut.
#>

param(
    [switch]$SkipShortcut,
    [switch]$SkipApiKeyPrompt
)

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RootDir

Write-Host "=== J.A.R.V.I.S. Windows Installer (Dev / Personal PC) ===" -ForegroundColor Cyan
Write-Host "Project root: $RootDir"

function Ensure-Command($cmd, $wingetId, $friendlyName) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) {
        return $true
    }
    Write-Host "$friendlyName not found. Attempting install via winget..." -ForegroundColor Yellow
    if (-not (Get-Command "winget" -ErrorAction SilentlyContinue)) {
        throw "$friendlyName is required and winget is unavailable. Please install $friendlyName manually and re-run."
    }
    winget install --id $wingetId --exact --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install $friendlyName via winget."
    }
    Write-Host "$friendlyName installed. Please open a new PowerShell window and re-run install.bat so PATH refreshes." -ForegroundColor Green
    exit 0
}

Ensure-Command "python" "Python.Python.3.12" "Python 3.12"
Ensure-Command "node" "OpenJS.NodeJS.LTS" "Node.js LTS"

# 1. Python virtual environment
$VenvPython = Join-Path $RootDir ".venv\Scripts\python.exe"
$VenvPip = Join-Path $RootDir ".venv\Scripts\pip.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "[1/5] Creating Python virtual environment (.venv)..." -ForegroundColor Cyan
    python -m venv .venv
} else {
    Write-Host "[1/5] Using existing .venv." -ForegroundColor DarkCyan
}

Write-Host "[2/5] Installing Python dependencies..." -ForegroundColor Cyan
& $VenvPython -m pip install --upgrade pip
& $VenvPip install -r requirements.txt

# 2. Pre-cache openWakeWord ONNX models
Write-Host "[3/5] Caching wake-word model (hey_jarvis)..." -ForegroundColor Cyan
& $VenvPython -c "from jarvis.wake_word import WakeWordDetector; WakeWordDetector().load(); print('Wake-word model ready.')"

# 3. Electron Desktop UI
Write-Host "[4/5] Installing Electron desktop dependencies..." -ForegroundColor Cyan
Push-Location (Join-Path $RootDir "desktop")
try {
    & npm.cmd install
    if ($LASTEXITCODE -ne 0) { throw "npm.cmd install failed." }
} finally {
    Pop-Location
}

# 4. Configure .env
$EnvFile = Join-Path $RootDir ".env"
$EnvExample = Join-Path $RootDir ".env.example"
if (-not (Test-Path $EnvFile) -and (Test-Path $EnvExample)) {
    Copy-Item $EnvExample $EnvFile
    if (-not $SkipApiKeyPrompt) {
        $apiKey = Read-Host "Enter your GEMINI_API_KEY (or press Enter to fill .env later)"
        if ($apiKey -and $apiKey.Trim().Length -gt 0) {
            (Get-Content $EnvFile) -replace "GEMINI_API_KEY=.*", "GEMINI_API_KEY=$($apiKey.Trim())" | Set-Content $EnvFile
            Write-Host "Saved GEMINI_API_KEY to .env." -ForegroundColor Green
        }
    }
}

# 5. Desktop shortcut
if (-not $SkipShortcut) {
    Write-Host "[5/5] Creating Desktop shortcut..." -ForegroundColor Cyan
    $DesktopDir = [Environment]::GetFolderPath("Desktop")
    $ShortcutPath = Join-Path $DesktopDir "Jarvis.lnk"
    $ElectronExe = Join-Path $RootDir "desktop\node_modules\electron\dist\electron.exe"
    $DesktopAppDir = Join-Path $RootDir "desktop"

    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $ElectronExe
    $Shortcut.Arguments = "."
    $Shortcut.WorkingDirectory = $DesktopAppDir
    $Shortcut.Description = "Launch J.A.R.V.I.S. Windows Command HUD"
    $Shortcut.Save()
    Write-Host "Shortcut created: $ShortcutPath" -ForegroundColor Green
}

Write-Host "`nRunning verification check..." -ForegroundColor Cyan
& $VenvPython -m jarvis.main --check

Write-Host "`nSetup complete! Double-click 'Jarvis' on your Desktop or run 'npm.cmd start' in desktop/." -ForegroundColor Green
