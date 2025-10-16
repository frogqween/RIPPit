# Creates a desktop shortcut for RIPPit and sets a custom icon.
# Usage: Right-click this file and Run with PowerShell, or run the companion .bat.
param()
$ErrorActionPreference = 'Stop'

$proj = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'RIPPit.lnk'

# Determine the best launcher (we will call it via wscript.exe so Taskbar pin works)
$launcher = Join-Path $proj 'Start RIPPit.vbs'
if (-not (Test-Path $launcher)) {
  # Fallback to .bat if no .vbs
  $launcher = Join-Path $proj 'Start RIPPit.bat'
  if (-not (Test-Path $launcher)) {
    $launcher = Join-Path $proj 'run_web.pyw'
    if (-not (Test-Path $launcher)) { throw "Unable to locate a launcher (Start RIPPit.vbs/.bat or run_web.pyw) in $proj" }
  }
}

# We will point the shortcut Target to wscript.exe (an EXE is required for Taskbar pin)
$targetExe = Join-Path $env:WINDIR 'System32\wscript.exe'
$arguments = '"' + $launcher + '"'

# Prepare icon: prefer RIPPit.ico; if missing but RIPPit.png exists, generate .ico that embeds the PNG
$ico = Join-Path $proj 'RIPPit.ico'
$png = Join-Path $proj 'RIPPit.png'

function Convert-PngToIco([string]$pngPath, [string]$icoPath){
  Add-Type -AssemblyName System.Drawing
  $pngBytes = [System.IO.File]::ReadAllBytes($pngPath)
  # Build a minimal .ico that embeds the PNG (supported since Vista)
  $ms = New-Object System.IO.MemoryStream
  $bw = New-Object System.IO.BinaryWriter($ms)
  # ICONDIR header (6 bytes)
  $bw.Write([UInt16]0)   # reserved
  $bw.Write([UInt16]1)   # type = icon
  $bw.Write([UInt16]1)   # image count
  # Read image to get size (falls back to 256 if larger)
  $imgStream = [System.IO.MemoryStream]::new($pngBytes)
  $img = [System.Drawing.Image]::FromStream($imgStream)
  # Compute width/height with ICO rule: 0 means 256
  $wInt = [Math]::Min(256, [Math]::Max(0, $img.Width))
  $hInt = [Math]::Min(256, [Math]::Max(0, $img.Height))
  $wByte = if ($wInt -eq 256) { [byte]0 } else { [byte]$wInt }
  $hByte = if ($hInt -eq 256) { [byte]0 } else { [byte]$hInt }
  # ICONDIRENTRY (16 bytes)
  $bw.Write($wByte)      # width (0 means 256)
  $bw.Write($hByte)      # height (0 means 256)
  $bw.Write([byte]0)       # color count
  $bw.Write([byte]0)       # reserved
  $bw.Write([UInt16]1)     # planes
  $bw.Write([UInt16]32)    # bit count
  $bw.Write([UInt32]$pngBytes.Length)  # bytes in resource
  $bw.Write([UInt32]22)    # image offset (6+16)
  # PNG data
  $bw.Write($pngBytes, 0, $pngBytes.Length)
  $bw.Flush()
  [System.IO.File]::WriteAllBytes($icoPath, $ms.ToArray())
  $img.Dispose()
  $imgStream.Dispose()
}

if (-not (Test-Path $ico)) {
  if (Test-Path $png) {
    try { Convert-PngToIco -pngPath $png -icoPath $ico } catch { Write-Warning "Failed to convert PNG to ICO: $_" }
  }
}

$iconLocation = if (Test-Path $ico) { $ico } else { $launcher }

# Create the .lnk (Target must be EXE for Taskbar pin)
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($shortcutPath)
$sc.TargetPath = $targetExe
$sc.Arguments = $arguments
$sc.WorkingDirectory = $proj
$sc.IconLocation = $iconLocation
$sc.WindowStyle = 7
$sc.Description = 'RIPPit - Open the downloader UI'
# Set AppUserModelID to help Taskbar pin (optional)
try { $sc.AppUserModelID = 'com.rippit.app' } catch {}
$sc.Save()

Write-Host "Created shortcut:" $shortcutPath
Write-Host "Icon:" $iconLocation
Write-Host "You can now right-click the shortcut and choose 'Pin to taskbar'."
