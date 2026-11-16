<#
.SYNOPSIS
  Build standalone Windows installer (desktop\dist\Jarvis-Setup-0.1.0.exe)
  using PyInstaller + electron-builder. No Python or Node.js required on target PCs.
#>

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RootDir

$VenvPython = Join-Path $RootDir ".venv\Scripts\python.exe"
$VenvPip = Join-Path $RootDir ".venv\Scripts\pip.exe"

Write-Host "[1/4] Ensuring PyInstaller is installed in .venv..." -ForegroundColor Cyan
$prevErr = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $VenvPython -m PyInstaller --version *> $null
$hasPyInstaller = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prevErr
if (-not $hasPyInstaller) {
    & $VenvPip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Failed to install pyinstaller." }
}

Write-Host "[2/4] Ensuring openWakeWord models are cached..." -ForegroundColor Cyan
& $VenvPython -c "from jarvis.wake_word import WakeWordDetector; WakeWordDetector().load()"

Write-Host "[3/4] Bundling Python backend (jarvis-bridge.exe)..." -ForegroundColor Cyan
$DistBridge = Join-Path $RootDir "desktop\resources-build"
if (Test-Path $DistBridge) {
    Remove-Item -Recurse -Force $DistBridge
}
& $VenvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --console `
    --name jarvis-bridge `
    --distpath $DistBridge `
    --collect-data openwakeword `
    --collect-all onnxruntime `
    --collect-all sounddevice `
    --hidden-import jarvis.tools.desktop `
    --hidden-import jarvis.tools.productivity `
    --hidden-import jarvis.tools.developer `
    (Join-Path $RootDir "jarvis\bridge.py")

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

Write-Host "[4/4] Building Windows installer via electron-builder..." -ForegroundColor Cyan
$DistDir = Join-Path $RootDir "desktop\dist"
if (Test-Path $DistDir) {
    Get-ChildItem -Path $DistDir -Filter "Jarvis-Setup-*.exe*" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
}
Push-Location (Join-Path $RootDir "desktop")
try {
    & npm.cmd install
    if ($LASTEXITCODE -ne 0) { throw "npm.cmd install failed." }
    & npm.cmd run dist:win
    if ($LASTEXITCODE -ne 0) { throw "electron-builder failed." }
} finally {
    Pop-Location
}

$SetupExe = Join-Path $DistDir "Jarvis-Setup.exe"
$SetupZip = Join-Path $DistDir "Jarvis-Setup.zip"
if (Test-Path $SetupExe) {
    Compress-Archive -Path $SetupExe -DestinationPath $SetupZip -Force
}

Write-Host "`nInstaller built successfully in desktop\dist\" -ForegroundColor Green
Get-ChildItem $DistDir -Include "*.exe", "*.zip" -File | Select-Object Name, Length, FullName
