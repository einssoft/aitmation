<#
.SYNOPSIS
  Build and test the whole repository on Windows - the same gates as CI
  (core: ruff + pytest + solution checks; ui: npm ci + build; launcher:
  go vet + go test when Go is installed) - and optionally start the core
  with the built UI and the example solutions.

.EXAMPLE
  .\scripts\check.ps1                 # everything, stop at the first failure
  .\scripts\check.ps1 -SkipUi         # core (and launcher) only
  .\scripts\check.ps1 -Start          # after the checks: start the core on :8477
  .\scripts\check.ps1 -Start -SkipTests  # just start, no gates

.NOTES
  npm needs the registry: behind a corporate proxy, or `registry=https://registry.npmjs.org/`
  in ui\.npmrc. Nothing here writes outside the repo and the dev data
  directory (`_dev` next to the repo), so ~\.aitmation stays untouched.
#>
[CmdletBinding()]
param(
    [switch]$SkipUi,
    [switch]$SkipLauncher,
    [switch]$SkipTests,
    [switch]$Start,
    [int]$Port = 8477,
    [string]$DevDir = ""
)

$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not $DevDir) { $DevDir = Join-Path (Split-Path $repo -Parent) "_dev" }
$failed = @()

function Step($title, [scriptblock]$body) {
    Write-Host ""
    Write-Host "==> $title" -ForegroundColor Cyan
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & $body
    if ($LASTEXITCODE -ne 0) { throw "$title failed (exit $LASTEXITCODE)" }
    Write-Host ("    ok ({0:n1}s)" -f $sw.Elapsed.TotalSeconds) -ForegroundColor Green
}

function Require($cmd, $hint) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "$cmd not found - $hint" }
}

Push-Location $repo
try {
    if (-not $SkipTests) {
        # ---- core -----------------------------------------------------------
        Require uv "install uv: https://docs.astral.sh/uv/"
        Push-Location core
        try {
            Step "core: uv sync"        { uv sync --extra mcp }
            Step "core: ruff check"     { uv run ruff check . }
            Step "core: license boundary ee/" { uv run python ..\scripts\check_ee_boundary.py }
            Step "core: pytest"         { uv run pytest -q }
            Step "core: validate bundles (format 1)" {
                $bundles = Get-ChildItem ..\apps -Directory | Where-Object { Test-Path (Join-Path $_.FullName "app.toml") } | ForEach-Object { $_.FullName }
                if ($bundles) { uv run python -m aitmation_core validate-app @bundles } else { Write-Host "    (none)" }
            }
            Step "core: validate solutions (format 2)" {
                $solutions = @(Get-ChildItem ..\apps, ..\docs\examples -Directory | Where-Object { Test-Path (Join-Path $_.FullName "solution.toml") } | ForEach-Object { $_.FullName })
                uv run python -m aitmation_core validate-solution @solutions
            }
            Step "core: proving runs"   {
                foreach ($s in @(Get-ChildItem ..\apps, ..\docs\examples -Directory | Where-Object { Test-Path (Join-Path $_.FullName "solution.toml") } | ForEach-Object { $_.FullName })) {
                    uv run python -m aitmation_core test $s
                    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
                }
            }
        } finally { Pop-Location }

        # ---- ui -------------------------------------------------------------
        if (-not $SkipUi) {
            Require npm "install Node.js 22+"
            Push-Location ui
            try {
                Step "ui: npm ci"    { npm ci --no-audit --no-fund }
                Step "ui: build (i18n gate + tsc + vite)" { npm run build }
            } finally { Pop-Location }
        }

        # ---- launcher -------------------------------------------------------
        if (-not $SkipLauncher) {
            if (Get-Command go -ErrorAction SilentlyContinue) {
                Push-Location launcher
                try {
                    Step "launcher: gofmt" {
                        $unformatted = gofmt -l .
                        if ($unformatted) { Write-Host $unformatted; exit 1 }
                    }
                    Step "launcher: go vet"  { go vet ./... }
                    Step "launcher: go test" { go test ./... }
                } finally { Pop-Location }
            } else {
                Write-Host ""
                Write-Host "==> launcher: skipped (go not installed)" -ForegroundColor Yellow
            }
        }

        Write-Host ""
        Write-Host "All checks passed." -ForegroundColor Green
    }

    # ---- start -------------------------------------------------------------
    if ($Start) {
        $apps = Join-Path $DevDir "apps"
        New-Item -ItemType Directory -Force $apps | Out-Null
        foreach ($dir in @(Get-ChildItem (Join-Path $repo "apps"), (Join-Path $repo "docs\examples") -Directory | Where-Object { Test-Path (Join-Path $_.FullName "solution.toml") })) {
            $name = $dir.Name
            if (Test-Path (Join-Path $apps $name)) { Remove-Item -Recurse -Force (Join-Path $apps $name) }
            Copy-Item -Recurse -Force $dir.FullName (Join-Path $apps $name)
        }
        $env:AITMATION_DATA_DIR      = $DevDir
        $env:AITMATION_TEMPLATES_DIR = (Join-Path $repo "core\templates")
        $env:AITMATION_APPS_DIR      = $apps
        $env:AITMATION_PORT          = "$Port"
        $dist = Join-Path $repo "ui\dist"
        if (Test-Path $dist) { $env:AITMATION_UI_DIR = $dist } else { Write-Host "ui\dist missing - API only" -ForegroundColor Yellow }
        Write-Host ""
        Write-Host "==> starting core on http://127.0.0.1:$Port/app/  (data: $DevDir)" -ForegroundColor Cyan
        Write-Host "    Werkstatt -> Loesungen needs 'Erweiterte Ansicht' (top right). Ctrl+C stops."
        Push-Location core
        try { uv run python -m aitmation_core } finally { Pop-Location }
    }
} catch {
    Write-Host ""
    Write-Host "FAILED: $_" -ForegroundColor Red
    exit 1
} finally {
    Pop-Location
}
