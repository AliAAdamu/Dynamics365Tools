# Builds a self-contained Windows distributable using PyInstaller.
# Output: dist\FabricRowCounter\FabricRowCounter.exe (one-folder bundle).
#
# Prerequisites:
#   - install.ps1 has been run successfully (.venv exists with all deps)
#   - The original CLI lives at ..\fabric-row-counter\count_rows.py
#
# Note: the resulting bundle is ~150-200 MB. ODBC Driver 18 is NOT bundled
# (it's an OS-level driver) — the README tells the end user to install it.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Test-Path .venv)) {
    throw "Run .\install.ps1 first."
}

$cliSrc = Resolve-Path ..\fabric-row-counter\count_rows.py
Write-Host "Staging count_rows.py from $cliSrc..." -ForegroundColor Cyan
Copy-Item $cliSrc .\count_rows.py -Force

Write-Host "Installing PyInstaller into venv..." -ForegroundColor Cyan
.\.venv\Scripts\python.exe -m pip install --quiet "pyinstaller>=6.10"

# Clean previous build artifacts
Remove-Item -Recurse -Force build, dist, FabricRowCounter.spec -ErrorAction SilentlyContinue

Write-Host "Building (this can take 3-5 minutes)..." -ForegroundColor Cyan
.\.venv\Scripts\python.exe -m PyInstaller `
  --name FabricRowCounter `
  --noconfirm `
  --clean `
  --add-data "app.py;." `
  --add-data "count_rows.py;." `
  --collect-all streamlit `
  --collect-all altair `
  --collect-all pandas `
  --collect-data azure `
  --hidden-import pyodbc `
  --hidden-import azure.identity `
  --hidden-import azure.identity._credentials.browser `
  --hidden-import dotenv `
  launcher.py

# Don't ship the temporary copy in source control
Remove-Item .\count_rows.py -ErrorAction SilentlyContinue

if (Test-Path dist\FabricRowCounter\FabricRowCounter.exe) {
    $size = (Get-ChildItem dist\FabricRowCounter -Recurse | Measure-Object Length -Sum).Sum / 1MB
    Write-Host ("`nSuccess. Bundle: dist\FabricRowCounter\  (~{0:N0} MB)" -f $size) -ForegroundColor Green
    Write-Host "Distribute by zipping the entire 'dist\FabricRowCounter' folder." -ForegroundColor Green
    Write-Host "End user runs: FabricRowCounter.exe   (browser opens automatically)" -ForegroundColor Green
} else {
    throw "Build failed - check the log above."
}
