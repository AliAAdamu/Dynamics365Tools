#Requires -Version 5.1
<#
.SYNOPSIS
  Start the D365 DMF Tester web application.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$venvActivate = Join-Path $root '.venv\Scripts\Activate.ps1'
if (-not (Test-Path $venvActivate)) {
    Write-Error "Virtual environment not found.  Run .\install.ps1 first."
    exit 1
}

. $venvActivate

$port = if ($env:PORT) { $env:PORT } else { '5000' }

Write-Host "`n=== D365 DMF Tester ===" -ForegroundColor Cyan
Write-Host "  URL  : http://localhost:$port" -ForegroundColor Green
Write-Host "  Press Ctrl+C to stop.`n"

# Open browser after a short delay to let Flask start
Start-Job -ScriptBlock {
    param($url)
    Start-Sleep -Seconds 2
    Start-Process $url
} -ArgumentList "http://localhost:$port" | Out-Null

python app.py
