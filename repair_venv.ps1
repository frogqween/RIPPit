$ErrorActionPreference = 'Stop'
$base = 'D:\RIPPit'
if (!(Test-Path -LiteralPath $base)) { throw "Folder not found: $base" }
# Choose Python launcher
$py = 'py'
try { Get-Command py -ErrorAction Stop | Out-Null } catch { $py = 'python' }
# Create venv if missing
if (!(Test-Path -LiteralPath (Join-Path $base '.venv'))) {
  & $py -m venv (Join-Path $base '.venv')
}
# Install deps
& (Join-Path $base '.venv\Scripts\python.exe') -m pip install -U pip
& (Join-Path $base '.venv\Scripts\python.exe') -m pip install -r (Join-Path $base 'requirements.txt')
Write-Output 'Venv repaired and dependencies installed.'
