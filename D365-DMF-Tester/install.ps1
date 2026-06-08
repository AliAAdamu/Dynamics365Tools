#Requires -Version 5.1
<#
.SYNOPSIS
  Install D365 DMF Tester dependencies into a Python virtual environment.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "`n=== D365 DMF Tester — Install ===" -ForegroundColor Cyan

# Check Python
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $python) {
    Write-Error "Python 3.9+ is required but was not found on PATH."
    exit 1
}

$ver = & $python.Source --version 2>&1
Write-Host "Using $ver at $($python.Source)" -ForegroundColor Green

# Create venv
$venvDir = Join-Path $root '.venv'
if (-not (Test-Path $venvDir)) {
    Write-Host "`nCreating virtual environment…"
    & $python.Source -m venv $venvDir
}

# Activate
$activate = Join-Path $venvDir 'Scripts\Activate.ps1'
. $activate

# Upgrade pip silently
python -m pip install --upgrade pip --quiet

# Install requirements
Write-Host "`nInstalling requirements…" -ForegroundColor Cyan
pip install -r requirements.txt

# Create data/uploads directories
@('data','data\results','uploads') | ForEach-Object {
    $d = Join-Path $root $_
    if (-not (Test-Path $d)) {
        New-Item -ItemType Directory -Path $d | Out-Null
        Write-Host "  Created $_"
    }
}

Write-Host "`n✔  Installation complete." -ForegroundColor Green
Write-Host "   Run the app with:  .\run.ps1" -ForegroundColor Yellow
