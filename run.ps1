# PowerShell setup helper for Windows
param(
  [switch]$NoInstall
)

$ErrorActionPreference = 'Stop'

$base = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $base

# Detect Python launcher or python
$python = 'py'
try { Get-Command py -ErrorAction Stop | Out-Null } catch { $python = 'python' }

# Create venv if needed
if (!(Test-Path .venv)) {
  & $python -m venv .venv
}

$venvPy = Join-Path .venv 'Scripts\python.exe'

if (-not $NoInstall) {
  & $venvPy -m pip install -U pip
  & $venvPy -m pip install -r requirements.txt
}

Write-Host "Ready. Start the server with:"
Write-Host ".\.venv\Scripts\python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload"
