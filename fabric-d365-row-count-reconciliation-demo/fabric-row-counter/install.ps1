<#
.SYNOPSIS
  Installs the Fabric Row Counter app: creates a Python virtual environment,
  installs dependencies, and seeds .env from .env.example.

.NOTES
  Requires:
    - Python 3.10+ on PATH
    - ODBC Driver 18 for SQL Server installed:
      https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server

.EXAMPLE
  .\install.ps1
  .\install.ps1 -Recreate
#>
[CmdletBinding()]
param(
    [string]$VenvPath = ".venv",
    [switch]$Recreate
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "    $msg" -ForegroundColor Yellow }

# 1. Locate Python
Write-Step "Checking for Python 3.10+"
$python = $null
foreach ($candidate in @("py -3", "python", "python3")) {
    try {
        $parts = $candidate.Split(" ", 2)
        $exe = $parts[0]
        $args = if ($parts.Count -gt 1) { @($parts[1], "--version") } else { @("--version") }
        $ver = & $exe @args 2>&1
        if ($LASTEXITCODE -eq 0 -and $ver -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 10)) {
                $python = $candidate
                Write-Ok "Found $ver via '$candidate'"
                break
            }
        }
    } catch { }
}
if (-not $python) {
    throw "Python 3.10+ not found on PATH. Install from https://www.python.org/downloads/ and re-run."
}

# 2. Create venv
if ($Recreate -and (Test-Path $VenvPath)) {
    Write-Step "Removing existing virtual environment at $VenvPath"
    Remove-Item -Recurse -Force $VenvPath
}

if (-not (Test-Path $VenvPath)) {
    Write-Step "Creating virtual environment at $VenvPath"
    $parts = $python.Split(" ", 2)
    if ($parts.Count -gt 1) {
        & $parts[0] $parts[1] -m venv $VenvPath
    } else {
        & $parts[0] -m venv $VenvPath
    }
    if ($LASTEXITCODE -ne 0) { throw "Failed to create virtual environment." }
    Write-Ok "Virtual environment created."
} else {
    Write-Ok "Reusing existing virtual environment at $VenvPath."
}

$venvPython = Join-Path $VenvPath "Scripts\python.exe"
if (-not (Test-Path $venvPython)) { throw "Virtual environment Python not found at $venvPython." }

# 3. Upgrade pip + install requirements
Write-Step "Upgrading pip"
& $venvPython -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }

Write-Step "Installing dependencies from requirements.txt"
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency install failed." }
Write-Ok "Dependencies installed."

# 4. Seed .env from .env.example
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Ok ".env created from .env.example - please edit it with your Fabric endpoint, database, and tables."
    } else {
        Write-Warn2 ".env.example not found; skipping .env creation."
    }
} else {
    Write-Ok ".env already present; left untouched."
}

# 5. ODBC Driver 18 check (best-effort)
Write-Step "Checking for ODBC Driver 18 for SQL Server"
try {
    $drivers = (Get-OdbcDriver -ErrorAction Stop | Where-Object { $_.Name -like "*ODBC Driver 18 for SQL Server*" })
    if ($drivers) {
        Write-Ok "ODBC Driver 18 for SQL Server is installed."
    } else {
        Write-Warn2 "ODBC Driver 18 for SQL Server NOT found."
        Write-Warn2 "Download: https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server"
    }
} catch {
    Write-Warn2 "Could not query ODBC drivers (Get-OdbcDriver unavailable). Ensure ODBC Driver 18 is installed."
}

Write-Host ""
Write-Host "Install complete." -ForegroundColor Green
Write-Host "Next steps:" -ForegroundColor Green
Write-Host "  1. Edit .env with your Fabric SQL endpoint, database name, and table list."
Write-Host "  2. Activate the venv:  .\$VenvPath\Scripts\Activate.ps1"
Write-Host "  3. Run the app:        python count_rows.py"
