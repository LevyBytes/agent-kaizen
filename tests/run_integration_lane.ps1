#Requires -Version 5.1
<#
.SYNOPSIS
Owner-gated integration lane for the real-sync-server labs; never run in PR CI.

.DESCRIPTION
Runs each fleet lab with a real local `tursodb --sync-server`, scratch planes, and real Git where required. Reports a per-lab PASS/FAIL/SKIP plus a lane verdict. Exit 0 means all labs passed, 1 means any lab failed or was missing, and 2 means a prerequisite was absent. PR CI stays hermetic.

.PARAMETER PythonExe
Explicit shared-venv interpreter path; otherwise derived from DEVROOT.

.PARAMETER TursodbExe
Explicit tursodb path; otherwise derived from DEVROOT.

.EXAMPLE
powershell -NoProfile -File tests\run_integration_lane.ps1
#>
[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [string]$TursodbExe = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if (-not $TursodbExe) {
    if ([string]::IsNullOrWhiteSpace($env:DEVROOT)) {
        Write-Host "lane verdict: SKIP (pass -TursodbExe or set DEVROOT)"
        exit 2
    }
    $TursodbExe = Join-Path $env:DEVROOT "tools\tursodb\tursodb.exe"
}
if (-not (Test-Path -LiteralPath $TursodbExe -PathType Leaf)) {
    Write-Host "lane verdict: SKIP (tursodb not found at $TursodbExe)"
    exit 2
}
if (-not $PythonExe) {
    if (-not [string]::IsNullOrWhiteSpace($env:DEVROOT)) { $PythonExe = Join-Path $env:DEVROOT "Python\venvs\kaizen\Scripts\python.exe" }
}
if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "python.exe not found: pass -PythonExe or set DEVROOT (see setup\SETUP.md)."
}

$labs = @(
    "tests\integration\m9_lab.py",
    "tests\integration\m15_lab.py",
    "tests\integration\m17_lab.py"
)

$results = @()
foreach ($lab in $labs) {
    $path = Join-Path $repo $lab
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        $results += [pscustomobject]@{ lab = $lab; verdict = "MISSING" }
        continue
    }
    Write-Host "=== $lab ==="
    $labArgs = @($path, "--tursodb-exe", $TursodbExe)
    $code = $null
    & $PythonExe @labArgs
    $code = $LASTEXITCODE
    $verdict = switch ($code) {
        0 { "PASS" }
        2 { "SKIP" }
        default { "FAIL" }
    }
    Write-Host "=== end $lab ($verdict) ==="
    $results += [pscustomobject]@{ lab = $lab; verdict = $verdict }
}

Write-Host ""
Write-Host "=== integration lane summary ==="
$results | Format-Table -AutoSize | Out-Host
if ($results | Where-Object { $_.verdict -eq "FAIL" -or $_.verdict -eq "MISSING" }) {
    Write-Host "lane verdict: FAIL"
    exit 1
}
Write-Host "lane verdict: PASS"
exit 0
