#Requires -Version 5.1
<#
.SYNOPSIS
  Populate the sibling SKILLS store and link its skills into this repo (Windows).

.DESCRIPTION
  Optionally clones or updates a skills store, then creates per-skill directory
  junctions into both .agents\skills and .claude\skills. Re-running repairs
  dangling junctions but is add-only: removed store skills are not pruned. A
  populated non-git store is never overwritten by a clone.

.PARAMETER StoreUrl
  Optional Git URL used to clone or update the shared skills store.
.PARAMETER DevRoot
  DEVROOT override; defaults through the shared installer resolution contract.
.PARAMETER RepoRoot
  Project receiving .agents and .claude skill junctions.
.PARAMETER PythonExe
  Python used only for optional skill-index regeneration.
.PARAMETER PlanOnly
  Print the resolved plan without external actions or writes.
.PARAMETER ListSteps
  Print the static plan and exit.
.PARAMETER EmitPlanJson
  Optional path for the deterministic plan snapshot.
.PARAMETER SelfTest
  Exercise step shape without external actions.
.PARAMETER NoProgressHeader
  Suppress shared progress/dashboard output.
.PARAMETER NoNetwork
  Deny store clone/pull network activity.
.PARAMETER NoExternalActions
  Deny native commands and junction creation through shared safety gates.
.PARAMETER NoUserEnvWrites
  Shared selector; this linker performs no user-environment persistence.
.PARAMETER AssumeYes
  Shared noninteractive selector; this linker has no confirmation prompt.
.PARAMETER NoInput
  Disable interactive input through the shared installer contract.
.PARAMETER NoPause
  Skip the closing prompt.
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

$missingSkillsMessage = 'No skills found at {0}. Pass -StoreUrl <git-url> or populate the store first.'

function Wait-AkClose {
    if (-not $NoPause) { Read-Host 'Press Enter to close' | Out-Null }
}

if (-not $RepoRoot) { $RepoRoot = Split-Path $PSScriptRoot -Parent }
$RepoRoot = Resolve-AkFullPath $RepoRoot
$resolvedDevRoot = Resolve-AkDevRoot -DevRoot $DevRoot -RepoRoot $RepoRoot -NoInput:$NoInput
$store = Join-Path $resolvedDevRoot 'SKILLS'
$skillsDir = Join-Path $store 'skills'
$steps = @(
    [pscustomobject]@{ Id='preflight'; Name='Resolve store paths and validate inputs' },
    [pscustomobject]@{ Id='store'; Name='Clone or update optional skills store' },
    [pscustomobject]@{ Id='links'; Name='Create .agents and .claude skill junctions' },
    [pscustomobject]@{ Id='index'; Name='Preview skill index reconciliation when skill-drafting is available' }
)
Initialize-AkInstaller -Name 'Agent Kaizen skill linker' -RepoRoot $RepoRoot -DevRoot $resolvedDevRoot -Steps $steps -PlanOnly:($PlanOnly -or $ListSteps) -NoProgressHeader:$NoProgressHeader -NoNetwork:$NoNetwork -NoExternalActions:$NoExternalActions -NoUserEnvWrites:$NoUserEnvWrites -AssumeYes:$AssumeYes -NoInput:$NoInput -SelfTest:$SelfTest

if ($ListSteps -or $PlanOnly) {
    Show-AkPlan
    if (-not [string]::IsNullOrWhiteSpace($EmitPlanJson)) { Write-AkPlanJson -Path $EmitPlanJson }
    if (-not $ListSteps) { Wait-AkClose }
    exit 0
}
if ($SelfTest) { Show-AkPlan }

try {
    Invoke-AkStep -Id 'preflight' -Name 'Resolve store paths and validate inputs' -ScriptBlock {
        # Echo resolved roots and fail when neither a URL nor an existing store can supply skills.
        Write-Host ("DEVROOT : {0}" -f $resolvedDevRoot)
        Write-Host ("RepoRoot: {0}" -f $RepoRoot)
        Write-Host ("Store   : {0}" -f $store)
        if ([string]::IsNullOrWhiteSpace($StoreUrl) -and -not (Test-Path -LiteralPath $skillsDir)) {
            throw ($missingSkillsMessage -f $skillsDir)
        }
    }

    Invoke-AkStep -Id 'store' -Name 'Clone or update optional skills store' -ScriptBlock {
        # Pull a git store, clone into an empty location, or refuse populated non-git content.
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
                $real = Get-ChildItem -LiteralPath $skillsDir -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne '.gitkeep' } | Select-Object -First 1
                if ($null -ne $real) { $hasContent = $true }
            }
            if (Test-Path -LiteralPath $store) {
                $rootReal = Get-ChildItem -LiteralPath $store -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -notin @('.gitkeep','skills') } | Select-Object -First 1
                if ($null -ne $rootReal) { $hasContent = $true }
            }
            if ($hasContent) { throw "SKILLS already has content at $store; pull or manage it manually instead of cloning over it." }
            New-AkDirectory $store
            Invoke-AkNative -Exe $git.Source -Arguments @('clone',$StoreUrl,$store) -ActivityNote 'Cloning the selected skills store.'
        }
    }

    Invoke-AkStep -Id 'links' -Name 'Create .agents and .claude skill junctions' -ScriptBlock {
        Assert-AkExternalAllowed 'skill junction creation'
        if (-not (Test-Path -LiteralPath $skillsDir)) {
            throw ($missingSkillsMessage -f $skillsDir)
        }
        $mirrors = @((Join-Path (Join-Path $RepoRoot '.agents') 'skills'), (Join-Path (Join-Path $RepoRoot '.claude') 'skills'))
        foreach ($m in $mirrors) { New-AkDirectory $m }
        $eligible = 0
        $created = 0
        $failed = 0
        foreach ($skill in (Get-ChildItem -LiteralPath $skillsDir -Directory -ErrorAction Stop)) {
            if (-not (Test-Path -LiteralPath (Join-Path $skill.FullName 'SKILL.md'))) { continue }
            $eligible += 1
            foreach ($m in $mirrors) {
                $link = Join-Path $m $skill.Name
                try {
                    if (Test-Path -LiteralPath $link) { continue }
                    $stale = Get-ChildItem -LiteralPath $m -Force -ErrorAction Stop | Where-Object { $_.Name -ceq $skill.Name } | Select-Object -First 1
                    if ($null -ne $stale) {
                        if (-not ($stale.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { continue }
                        Remove-Item -LiteralPath $stale.FullName -Force -ErrorAction Stop
                    }
                    New-Item -ItemType Junction -Path $link -Target $skill.FullName -ErrorAction Stop | Out-Null
                    $created += 1
                    Write-Host ("linked: {0} -> {1}" -f $link, $skill.FullName)
                } catch {
                    $failed += 1
                    Write-Warning ("link failed: {0}: {1}" -f $link, $_.Exception.Message)
                }
            }
        }
        Write-Host ("{0} eligible skill(s); {1} junction(s) created; {2} link failure(s)." -f $eligible, $created, $failed) -ForegroundColor Green
    }

    Invoke-AkStep -Id 'index' -Name 'Preview skill index reconciliation when skill-drafting is available' -ScriptBlock {
        $builder = Join-Path $skillsDir 'skill-drafting\scripts\skill_builder.py'
        if (-not (Test-Path -LiteralPath $builder)) {
            Write-Host 'INDEX.md not regenerated: skill-drafting\scripts\skill_builder.py not found in the store.' -ForegroundColor DarkGray
            return
        }
        $py = Resolve-AkPythonExe -PythonExe $PythonExe -DevRoot $resolvedDevRoot
        $indexRoot = Join-Path $RepoRoot '.claude\skills'
        $indexMirror = Join-Path $RepoRoot '.agents\skills'
        Invoke-AkNative -Exe $py -Arguments @($builder,'index','plan',$indexRoot,'--mirror',$indexMirror) -ActivityNote 'Previewing the skill index reconciliation; this step does not write.'
        Write-Host ("Apply only after reviewing the plan: {0} {1} index apply {2} --mirror {3} --confirm-plan PLAN_SHA256" -f $py, $builder, $indexRoot, $indexMirror) -ForegroundColor Yellow
    }
} catch {
    Write-Host ''
    Write-Host ("FAILED: {0}" -f $_.Exception.Message) -ForegroundColor Red
    Wait-AkClose
    exit 1
}

Wait-AkClose
exit 0
