# Undo hidden folder layout: move contents of 'hidden' back to project root
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$hidden = Join-Path $root 'hidden'

if (Test-Path -LiteralPath $hidden) {
  foreach ($item in Get-ChildItem -Force -LiteralPath $hidden) {
    $dest = Join-Path $root $item.Name
    if (Test-Path -LiteralPath $dest) {
      if ($item.PSIsContainer) {
        # Merge directories
        foreach ($child in Get-ChildItem -Force -LiteralPath $item.FullName) {
          $childDest = Join-Path $dest $child.Name
          if (Test-Path -LiteralPath $childDest) {
            try { Remove-Item -Recurse -Force -LiteralPath $childDest -ErrorAction SilentlyContinue } catch {}
          }
          Move-Item -Force -LiteralPath $child.FullName -Destination $childDest
        }
        try { Remove-Item -Recurse -Force -LiteralPath $item.FullName -ErrorAction SilentlyContinue } catch {}
      } else {
        try { Remove-Item -Force -LiteralPath $dest -ErrorAction SilentlyContinue } catch {}
        Move-Item -Force -LiteralPath $item.FullName -Destination $dest
      }
    } else {
      Move-Item -Force -LiteralPath $item.FullName -Destination $dest
    }
  }
  try { attrib -h (Join-Path $root 'hidden') } catch {}
  try { Remove-Item -Recurse -Force -LiteralPath $hidden -ErrorAction SilentlyContinue } catch {}
  Write-Output "Restored contents from 'hidden' and removed folder."
} else {
  Write-Output "No 'hidden' folder present; nothing to restore."
}
