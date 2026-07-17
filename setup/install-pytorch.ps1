#Requires -Version 5.1
<#
.SYNOPSIS
Install the opt-in PyTorch extra for in-process sentence-transformers embeddings and semantic chunking.
.DESCRIPTION
Installs into the explicit interpreter or required shared Kaizen venv. CUDA is the default; -Cpu selects CPU-only torch. The CUDA 12.1 index is a conservative compatibility default, not GPU auto-detection; override -CudaIndex when the local driver/GPU requires a newer wheel such as cu128.
.PARAMETER DevRoot
DEVROOT containing the shared Kaizen venv and external model cache.
.PARAMETER PythonExe
Explicit interpreter; otherwise the shared Kaizen venv is required.
.PARAMETER Cpu
Install CPU-only torch instead of the CUDA default.
.PARAMETER CudaIndex
Torch CUDA wheel index used when -Cpu is absent.
.PARAMETER PlanOnly
Print the step plan without running external actions.
.PARAMETER ListSteps
Print the step plan and exit.
.PARAMETER EmitPlanJson
Write the plan JSON when listing, planning, or self-testing.
.PARAMETER SelfTest
Validate step shapes without running external actions.
.PARAMETER NoProgressHeader
Suppress the repainting progress dashboard.
.PARAMETER NoNetwork
Block pip package installation.
.PARAMETER NoExternalActions
Block directory creation and native commands.
.PARAMETER NoUserEnvWrites
Shared installer selector; this script sets HF_HOME only in the current process and never persists user variables.
.PARAMETER AssumeYes
Shared non-interactive installer selector; this script has no prompt.
.PARAMETER NoInput
Prevent interactive DEVROOT selection.
.EXAMPLE
setup\install-pytorch.ps1 -PlanOnly -NoNetwork -NoExternalActions -NoUserEnvWrites -NoInput
#>
[CmdletBinding()]
param(
    [string]$DevRoot,
    [string]$PythonExe,
    [switch]$Cpu,
    [string]$CudaIndex = 'https://download.pytorch.org/whl/cu121',
    [switch]$PlanOnly,
    [switch]$ListSteps,
    [string]$EmitPlanJson,
    [switch]$SelfTest,
    [switch]$NoProgressHeader,
    [switch]$NoNetwork,
    [switch]$NoExternalActions,
    [switch]$NoUserEnvWrites,
    [switch]$AssumeYes,
    [switch]$NoInput
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'installer-common.ps1')

$repoRoot = Split-Path -Parent $PSScriptRoot
$resolvedDevRoot = Resolve-AkDevRoot -DevRoot $DevRoot -RepoRoot $repoRoot -NoInput:$NoInput
$steps = @(
    [pscustomobject]@{ Id='preflight'; Name='Resolve DEVROOT, Python, and model cache' },
    [pscustomobject]@{ Id='torch'; Name='Install or validate torch wheel' },
    [pscustomobject]@{ Id='extra'; Name='Install Agent Kaizen PyTorch extra' },
    [pscustomobject]@{ Id='summary'; Name='Print backend environment and verification command' }
)
Initialize-AkInstaller -Name 'Agent Kaizen PyTorch extra installer' -RepoRoot $repoRoot -DevRoot $resolvedDevRoot -Steps $steps -PlanOnly:($PlanOnly -or $ListSteps) -NoProgressHeader:$NoProgressHeader -NoNetwork:$NoNetwork -NoExternalActions:$NoExternalActions -NoUserEnvWrites:$NoUserEnvWrites -AssumeYes:$AssumeYes -NoInput:$NoInput -SelfTest:$SelfTest

if ($ListSteps -or $PlanOnly) {
    Show-AkPlan
    if (-not [string]::IsNullOrWhiteSpace($EmitPlanJson)) { Write-AkPlanJson -Path $EmitPlanJson }
    exit 0
}
if ($SelfTest) {
    Show-AkPlan
    if (-not [string]::IsNullOrWhiteSpace($EmitPlanJson)) { Write-AkPlanJson -Path $EmitPlanJson }
}

$script:AkPython = ''
$script:AkCache = Join-Path $resolvedDevRoot 'models'

Invoke-AkStep -Id 'preflight' -Name 'Resolve DEVROOT, Python, and model cache' -ScriptBlock {
    $script:AkPython = Resolve-AkPythonExe -PythonExe $PythonExe -DevRoot $resolvedDevRoot -RequireShared
    if (-not (Test-Path -LiteralPath $script:AkCache)) { Assert-AkExternalAllowed ("create model cache {0}" -f $script:AkCache) }
    New-AkDirectory $script:AkCache
    $env:HF_HOME = $script:AkCache
    Write-Host ("Installing into: {0}" -f $script:AkPython) -ForegroundColor Cyan
    Write-Host ("HF weight cache: {0}" -f $script:AkCache) -ForegroundColor Cyan
}

Invoke-AkStep -Id 'torch' -Name 'Install or validate torch wheel' -ScriptBlock {
    Assert-AkNetworkAllowed 'torch package installation'
    if ($Cpu) {
        Invoke-AkNative -Exe $script:AkPython -Arguments @('-m','pip','install','torch') -ActivityNote 'Installing CPU-only torch (selected via -Cpu; default is GPU/CUDA); pip output is logged and tailed while this runs.'
    } else {
        Invoke-AkNative -Exe $script:AkPython -Arguments @('-m','pip','install','torch','--index-url',$CudaIndex) -ActivityNote ('Installing CUDA torch (GPU-first default) from {0}; pip will report package download progress when available.' -f $CudaIndex)
    }
}

Invoke-AkStep -Id 'extra' -Name 'Install Agent Kaizen PyTorch extra' -ScriptBlock {
    $req = Join-Path $repoRoot 'requirements-pytorch.txt'
    if (-not (Test-Path -LiteralPath $req)) { throw "requirements file not found: $req" }
    Assert-AkNetworkAllowed 'PyTorch extra requirements installation'
    Invoke-AkNative -Exe $script:AkPython -Arguments @('-m','pip','install','-r',$req) -ActivityNote 'Installing sentence-transformers and pinned PyTorch extra dependencies.'
}

Invoke-AkStep -Id 'summary' -Name 'Print backend environment and verification command' -ScriptBlock {
    Write-Host ''
    Write-Host 'PyTorch embedding backend installed. Recommended session settings (HF_HOME already applied):' -ForegroundColor Green
    Write-Host ("  `$env:HF_HOME              = '{0}'" -f $script:AkCache)
    Write-Host "  `$env:KAIZEN_EMBED_BACKEND = 'sentence-transformers'"
    Write-Host "  `$env:KAIZEN_EMBED_MODEL   = 'codefuse-ai/F2LLM-v2-1.7B'"
    Write-Host ("  Verify: & '{0}' '{1}' B1 --json" -f $script:AkPython, (Join-Path $repoRoot 'kaizen.py'))
}
