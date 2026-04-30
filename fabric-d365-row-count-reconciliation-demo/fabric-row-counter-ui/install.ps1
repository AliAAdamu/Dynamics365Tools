# Installs the Streamlit UI for fabric-row-counter.
# Creates a .venv in this folder and installs dependencies.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$python = $null
foreach ($cmd in @("py -3", "python", "python3")) {
    try { & $cmd.Split()[0] $cmd.Split()[1..($cmd.Split().Count - 1)] --version 2>$null | Out-Null; $python = $cmd; break } catch {}
}
if (-not $python) { throw "Python 3.10+ not found in PATH." }

if (-not (Test-Path .venv)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    & $python.Split()[0] $python.Split()[1..($python.Split().Count - 1)] -m venv .venv
}

Write-Host "Installing dependencies..." -ForegroundColor Cyan
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host "`nReady. Launch the app with:" -ForegroundColor Green
Write-Host "  .\run.ps1" -ForegroundColor Yellow
