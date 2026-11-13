<#
.SYNOPSIS
  Start the core with the built UI and the example solutions on
  http://127.0.0.1:8477/app/ (dev data directory `_dev` next to the repo).
  Run .\scripts\check.ps1 once first, so ui\dist exists.

.EXAMPLE
  .\scripts\start.ps1
  .\scripts\start.ps1 -Port 8500
#>
[CmdletBinding()]
param(
    [int]$Port = 8477,
    [string]$DevDir = ""
)
$dist = Join-Path $PSScriptRoot "..\ui\dist"
if (-not (Test-Path $dist)) {
    Write-Host "ui\dist is missing - run .\scripts\check.ps1 once (builds the UI), or continue API-only." -ForegroundColor Yellow
}
& (Join-Path $PSScriptRoot "check.ps1") -Start -SkipTests -Port $Port -DevDir $DevDir
