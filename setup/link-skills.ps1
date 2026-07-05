#Requires -Version 5.1
<#
.SYNOPSIS
  Populate the sibling SKILLS store and link its skills into this repo (Windows).

.DESCRIPTION
  Optionally clones or updates a skills store, then creates per-skill directory
  junctions into both .agents\skills and .claude\skills. Re-running is safe.
#>
[CmdletBinding()]
param(
    [string]$StoreUrl,
    [string]$DevRoot,
    [string]$RepoRoot,
    [string]$PythonExe,
    [switch]$PlanOnly,
    [switch]$ListSteps,
    [string]$EmitPlanJson,
    [switch]$SelfTest,
    [switch]$NoProgressHeader,
    [switch]$NoNetwork,
    [switch]$NoExternalActions,
    [switch]$NoUserEnvWrites,
    [switch]$AssumeYes,
    [switch]$NoInput,
    [switch]$NoPause
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'installer-common.ps1')

if (-not $RepoRoot) { $RepoRoot = Split-Path $PSScriptRoot -Parent }
$RepoRoot = Resolve-AkFullPath $RepoRoot
$resolvedDevRoot = Resolve-AkDevRoot -DevRoot $DevRoot -RepoRoot $RepoRoot -NoInput:$NoInput
$store = Join-Path $resolvedDevRoot 'SKILLS'
$skillsDir = Join-Path $store 'skills'
$steps = @(
    [pscustomobject]@{ Id='preflight'; Name='Resolve store paths and validate inputs' },
    [pscustomobject]@{ Id='store'; Name='Clone or update optional skills store' },
    [pscustomobject]@{ Id='links'; Name='Create .agents and .claude skill junctions' },
    [pscustomobject]@{ Id='index'; Name='Regenerate skill index when skill-drafting is available' }
)
Initialize-AkInstaller -Name 'Agent Kaizen skill linker' -RepoRoot $RepoRoot -DevRoot $resolvedDevRoot -Steps $steps -PlanOnly:($PlanOnly -or $ListSteps) -NoProgressHeader:$NoProgressHeader -NoNetwork:$NoNetwork -NoExternalActions:$NoExternalActions -NoUserEnvWrites:$NoUserEnvWrites -AssumeYes:$AssumeYes -NoInput:$NoInput -SelfTest:$SelfTest

if ($ListSteps -or $PlanOnly) {
    Show-AkPlan
    if (-not [string]::IsNullOrWhiteSpace($EmitPlanJson)) { Write-AkPlanJson -Path $EmitPlanJson }
    if (-not $NoPause -and -not $ListSteps) { Read-Host 'Press Enter to close' | Out-Null }
    exit 0
}
if ($SelfTest) { Show-AkPlan }

try {
    Invoke-AkStep -Id 'preflight' -Name 'Resolve store paths and validate inputs' -ScriptBlock {
        Write-Host ("DEVROOT : {0}" -f $resolvedDevRoot)
        Write-Host ("RepoRoot: {0}" -f $RepoRoot)
        Write-Host ("Store   : {0}" -f $store)
        if ([string]::IsNullOrWhiteSpace($StoreUrl) -and -not (Test-Path -LiteralPath $skillsDir)) {
            throw "No skills found at $skillsDir. Pass -StoreUrl <git-url> or populate the store first."
        }
    }

    Invoke-AkStep -Id 'store' -Name 'Clone or update optional skills store' -ScriptBlock {
        if ([string]::IsNullOrWhiteSpace($StoreUrl)) {
            Write-Host 'No store URL supplied; using the existing local store.' -ForegroundColor DarkGray
            return
        }
        Assert-AkNetworkAllowed $StoreUrl
        $git = Get-Command git -ErrorAction SilentlyContinue
        if (-not $git) { throw 'git not found on PATH.' }
        if (Test-Path -LiteralPath (Join-Path $store '.git')) {
            Invoke-AkNative -Exe $git.Source -Arguments @('-C',$store,'pull','--ff-only') -ActivityNote 'Updating the existing skills store with git pull --ff-only.'
        } else {
            $hasContent = $false
            if (Test-Path -LiteralPath $skillsDir) {
                $real = @(Get-ChildItem -LiteralPath $skillsDir -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne '.gitkeep' } | Select-Object -First 1)
                if ($real.Count -gt 0) { $hasContent = $true }
            }
            if (Test-Path -LiteralPath $store) {
                $rootReal = @(Get-ChildItem -LiteralPath $store -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -notin @('.gitkeep','skills') } | Select-Object -First 1)
                if ($rootReal.Count -gt 0) { $hasContent = $true }
            }
            if ($hasContent) { throw "SKILLS already has content at $store; pull or manage it manually instead of cloning over it." }
            New-AkDirectory $store
            Invoke-AkNative -Exe $git.Source -Arguments @('clone',$StoreUrl,$store) -ActivityNote 'Cloning the selected skills store.'
        }
    }

    Invoke-AkStep -Id 'links' -Name 'Create .agents and .claude skill junctions' -ScriptBlock {
        if (-not (Test-Path -LiteralPath $skillsDir)) {
            throw "No skills found at $skillsDir. Pass -StoreUrl <git-url> or populate the store first."
        }
        $mirrors = @((Join-Path $RepoRoot '.agents\skills'), (Join-Path $RepoRoot '.claude\skills'))
        foreach ($m in $mirrors) { New-AkDirectory $m }
        $linked = 0
        foreach ($skill in (Get-ChildItem -LiteralPath $skillsDir -Directory -ErrorAction Stop)) {
            if (-not (Test-Path -LiteralPath (Join-Path $skill.FullName 'SKILL.md'))) { continue }
            foreach ($m in $mirrors) {
                $link = Join-Path $m $skill.Name
                if (Test-Path -LiteralPath $link) { continue }
                New-Item -ItemType Junction -Path $link -Target $skill.FullName | Out-Null
            }
            $linked += 1
            Write-Host ("linked: {0}" -f $skill.Name)
        }
        Write-Host ("{0} skill(s) linked into .agents\skills and .claude\skills." -f $linked) -ForegroundColor Green
    }

    Invoke-AkStep -Id 'index' -Name 'Regenerate skill index when skill-drafting is available' -ScriptBlock {
        $builder = Join-Path $skillsDir 'skill-drafting\scripts\skill_builder.py'
        if (-not (Test-Path -LiteralPath $builder)) {
            Write-Host 'INDEX.md not regenerated: skill-drafting\scripts\skill_builder.py not found in the store.' -ForegroundColor DarkGray
            return
        }
        $py = Resolve-AkPythonExe -PythonExe $PythonExe -DevRoot $resolvedDevRoot
        Invoke-AkNative -Exe $py -Arguments @($builder,'index',(Join-Path $RepoRoot '.claude\skills'),'--mirror',(Join-Path $RepoRoot '.agents\skills')) -ActivityNote 'Regenerating skill index files from the linked skill store.'
    }
} catch {
    Write-Host ''
    Write-Host ("FAILED: {0}" -f $_.Exception.Message) -ForegroundColor Red
    if (-not $NoPause) { Read-Host 'Press Enter to close' | Out-Null }
    exit 1
}

if (-not $NoPause) { Read-Host 'Press Enter to close' | Out-Null }
exit 0
