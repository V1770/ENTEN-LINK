<#
.SYNOPSIS
    One-click Windows build for Pioneer DJ Link.

.DESCRIPTION
    1. Creates / refreshes a local virtualenv in .venv-win
    2. Installs runtime requirements + PyInstaller
    3. Runs PyInstaller using packaging\windows\app.spec
    4. (Optional) Runs Inno Setup ISCC to produce PioneerDJLink-Setup.exe

.NOTES
    Requirements on the build machine:
      - Python 3.11 or 3.12 (3.13 also works, but Qt wheels lag occasionally)
      - Inno Setup 6 installed (https://jrsoftware.org/isdl.php) if you want
        the Setup.exe step.  Otherwise the dist\PioneerDJLink folder is
        already a portable build.

.EXAMPLE
    PS> cd "C:\path\to\Pioneer DJ Link"
    PS> powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1

.AUTHOR
    Vittorio Becker
#>

[CmdletBinding()]
param(
    [switch]$SkipInstaller,    # only produce the dist folder, no Setup.exe
    [switch]$Clean             # wipe build/ and dist/ first
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot   = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$venvDir    = Join-Path $repoRoot ".venv-win"
$venvPython = Join-Path $venvDir  "Scripts\python.exe"
$specFile   = Join-Path $repoRoot "packaging\windows\app.spec"
$issFile    = Join-Path $repoRoot "packaging\windows\installer.iss"
$reqFile    = Join-Path $repoRoot "packaging\windows\requirements-windows.txt"

Push-Location $repoRoot
try {
    if ($Clean) {
        Write-Host "==> Cleaning previous build artefacts" -ForegroundColor Cyan
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue build, dist
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $PSScriptRoot "Output")
    }

    if (-not (Test-Path $venvPython)) {
        Write-Host "==> Creating virtualenv at $venvDir" -ForegroundColor Cyan
        python -m venv $venvDir
    }

    Write-Host "==> Upgrading pip / wheel" -ForegroundColor Cyan
    & $venvPython -m pip install --upgrade pip wheel | Out-Host

    Write-Host "==> Installing runtime requirements" -ForegroundColor Cyan
    & $venvPython -m pip install -r $reqFile | Out-Host

    Write-Host "==> Installing PyInstaller" -ForegroundColor Cyan
    & $venvPython -m pip install "pyinstaller>=6.6" | Out-Host

    Write-Host "==> Running PyInstaller" -ForegroundColor Cyan
    & $venvPython -m PyInstaller --noconfirm --clean $specFile | Out-Host

    $exePath = Join-Path $repoRoot "dist\PioneerDJLink.exe"
    if (-not (Test-Path $exePath)) {
        throw "Build failed: $exePath not produced."
    }
    Write-Host "==> Portable build ready: $exePath" -ForegroundColor Green

    if ($SkipInstaller) {
        Write-Host "==> Skipping installer step (requested)." -ForegroundColor Yellow
        return
    }

    # Locate Inno Setup compiler
    $iscc = $null
    foreach ($candidate in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    )) {
        if (Test-Path $candidate) { $iscc = $candidate; break }
    }

    if (-not $iscc) {
        Write-Warning "Inno Setup not found. Install it from https://jrsoftware.org/isdl.php and re-run, or pass -SkipInstaller."
        return
    }

    Write-Host "==> Compiling installer with $iscc" -ForegroundColor Cyan
    Push-Location (Split-Path $issFile)
    try {
        & $iscc $issFile | Out-Host
    } finally {
        Pop-Location
    }

    $setupExe = Join-Path $PSScriptRoot "Output\PioneerDJLink-Setup.exe"
    if (Test-Path $setupExe) {
        Write-Host ""
        Write-Host "==> Installer ready:" -ForegroundColor Green
        Write-Host "    $setupExe" -ForegroundColor Green
    }
}
finally {
    Pop-Location
}
