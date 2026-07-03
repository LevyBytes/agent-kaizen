#requires -Version 5.1
<#
.SYNOPSIS
  Populate the sibling SKILLS store and link its skills into this repo (Windows).

.DESCRIPTION
  The public repo ships with NO skills. This helper optionally clones a skills
  store of your choosing into $DEVROOT\SKILLS, then creates a per-skill directory
  JUNCTION for every skill (a folder containing SKILL.md) found under
  $DEVROOT\SKILLS\skills\<name>, into BOTH .agents\skills\<name> and
  .claude\skills\<name>. Re-running is safe; existing links are left alone.

  Expected store layout:
    $DEVROOT\SKILLS\skills\<skill-name>\SKILL.md

.PARAMETER StoreUrl
  Optional git URL of a skills store to clone (or pull) into $DEVROOT\SKILLS.

.PARAMETER DevRoot
  Parent folder holding this repo and the SKILLS store. Defaults to $env:DEVROOT,
  else this repo's parent.

.NOTES
  Junctions need no admin rights. Public repo: https://github.com/LevyBytes/agent-kaizen
#>
[CmdletBinding()]
param(
    [string] $StoreUrl,
    [string] $DevRoot,
    [string] $RepoRoot,
    [switch] $NoPause
)
$ErrorActionPreference = 'Stop'

if (-not $RepoRoot) { $RepoRoot = Split-Path $PSScriptRoot -Parent }
if (-not $DevRoot) { if ($env:DEVROOT) { $DevRoot = $env:DEVROOT } else { $DevRoot = Split-Path $RepoRoot -Parent } }
$DevRoot   = [System.IO.Path]::GetFullPath($DevRoot)
$Store     = Join-Path $DevRoot 'SKILLS'
$SkillsDir = Join-Path $Store 'skills'

Write-Host '=== Agent Kaizen: link skills ===' -ForegroundColor White
Write-Host "  DEVROOT : $DevRoot"
Write-Host "  store   : $Store"

try {
    # 1. Optionally clone or update the skills store.
    if ($StoreUrl) {
        if (Test-Path (Join-Path $Store '.git')) {
            Write-Host '  Updating existing store (git pull)...'
            & git -C $Store pull --ff-only
            if ($LASTEXITCODE -ne 0) { Write-Warning 'git pull did not fast-forward; leaving the store as-is.' }
        } else {
            $hasContent = $false
            if (Test-Path $SkillsDir) {
                $real = Get-ChildItem $SkillsDir -Force | Where-Object { $_.Name -ne '.gitkeep' }
                if ($real) { $hasContent = $true }
            }
            if (Test-Path $Store) {
                $rootReal = Get-ChildItem $Store -Force | Where-Object { $_.Name -notin @('.gitkeep', 'skills') }
                if ($rootReal) { $hasContent = $true }
            }
            if ($hasContent) { throw "SKILLS already has content at $Store; pull or manage it manually instead of cloning over it." }
            if (Test-Path $Store) { Remove-Item -Recurse -Force $Store }
            & git clone $StoreUrl $Store
            if ($LASTEXITCODE -ne 0) { throw "git clone failed from $StoreUrl" }
        }
    }

    if (-not (Test-Path $SkillsDir)) {
        throw "No skills found at $SkillsDir. Pass -StoreUrl <git-url> or populate the store first."
    }

    # 2. Link every skill (a folder containing SKILL.md) into both mirrors.
    $mirrors = @((Join-Path $RepoRoot '.agents\skills'), (Join-Path $RepoRoot '.claude\skills'))
    foreach ($m in $mirrors) { New-Item -ItemType Directory -Path $m -Force | Out-Null }

    $linked = 0
    foreach ($skill in (Get-ChildItem $SkillsDir -Directory)) {
        if (-not (Test-Path (Join-Path $skill.FullName 'SKILL.md'))) { continue }
        foreach ($m in $mirrors) {
            $link = Join-Path $m $skill.Name
            if (Test-Path $link) { continue }
            New-Item -ItemType Junction -Path $link -Target $skill.FullName | Out-Null
        }
        $linked++
        Write-Host "  linked: $($skill.Name)"
    }
    Write-Host "  $linked skill(s) linked into .agents\skills and .claude\skills." -ForegroundColor Green

    # 3. Best-effort: regenerate INDEX.md if the store carries skill-drafting.
    $builder = Join-Path $SkillsDir 'skill-drafting\scripts\skill_builder.py'
    if (Test-Path $builder) {
        $py = Join-Path $DevRoot 'Python\venvs\kaizen\Scripts\python.exe'
        if (-not (Test-Path $py)) { $py = 'python' }
        & $py $builder index (Join-Path $RepoRoot '.claude\skills') --mirror (Join-Path $RepoRoot '.agents\skills')
    } else {
        Write-Host '  (INDEX.md not regenerated: skill-drafting\scripts\skill_builder.py not found in the store.)'
    }
} catch {
    Write-Host ''
    Write-Host ("FAILED: {0}" -f $_.Exception.Message) -ForegroundColor Red
    if (-not $NoPause) { Read-Host 'Press Enter to close' | Out-Null }
    exit 1
}

if (-not $NoPause) { Read-Host 'Press Enter to close' | Out-Null }
exit 0
