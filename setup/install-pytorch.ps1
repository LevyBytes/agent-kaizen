#Requires -Version 5.1
<#
  Install the OPT-IN PyTorch extra (in-process sentence-transformers embeddings + semantic chunking).

  Usage:
    setup\install-pytorch.ps1 [-Cpu] [-CudaIndex URL] [-DevRoot D:\dev] [-PythonExe path]
    setup\install-pytorch.ps1 -ListSteps
    setup\install-pytorch.ps1 -PlanOnly -NoNetwork -NoExternalActions
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
if ($SelfTest) { Show-AkPlan }

$script:AkPython = ''
$script:AkCache = Join-Path $resolvedDevRoot 'models'

Invoke-AkStep -Id 'preflight' -Name 'Resolve DEVROOT, Python, and model cache' -ScriptBlock {
    $script:AkPython = Resolve-AkPythonExe -PythonExe $PythonExe -DevRoot $resolvedDevRoot -RequireShared
    New-AkDirectory $script:AkCache
    $env:HF_HOME = $script:AkCache
    Write-Host ("Installing into: {0}" -f $script:AkPython) -ForegroundColor Cyan
    Write-Host ("HF weight cache: {0}" -f $script:AkCache) -ForegroundColor Cyan
}

Invoke-AkStep -Id 'torch' -Name 'Install or validate torch wheel' -ScriptBlock {
    if ($Cpu) {
        Invoke-AkNative -Exe $script:AkPython -Arguments @('-m','pip','install','torch') -ActivityNote 'Installing CPU torch (opt-out via -Cpu); pip output is logged and tailed while this runs.'
    } else {
        Invoke-AkNative -Exe $script:AkPython -Arguments @('-m','pip','install','torch','--index-url',$CudaIndex) -ActivityNote ('Installing CUDA torch (GPU-first default) from {0}; pip will report package download progress when available.' -f $CudaIndex)
    }
}

Invoke-AkStep -Id 'extra' -Name 'Install Agent Kaizen PyTorch extra' -ScriptBlock {
    $req = Join-Path $repoRoot 'requirements-pytorch.txt'
    if (-not (Test-Path -LiteralPath $req)) { throw "requirements file not found: $req" }
    Invoke-AkNative -Exe $script:AkPython -Arguments @('-m','pip','install','-r',$req) -ActivityNote 'Installing sentence-transformers and pinned PyTorch extra dependencies.'
}

Invoke-AkStep -Id 'summary' -Name 'Print backend environment and verification command' -ScriptBlock {
    Write-Host ''
    Write-Host 'PyTorch embedding backend installed. Current-session settings:' -ForegroundColor Green
    Write-Host ("  `$env:HF_HOME              = '{0}'" -f $script:AkCache)
    Write-Host "  `$env:KAIZEN_EMBED_BACKEND = 'sentence-transformers'"
    Write-Host "  `$env:KAIZEN_EMBED_MODEL   = 'codefuse-ai/F2LLM-v2-1.7B'"
    Write-Host ("  Verify: & '{0}' '{1}' B1 --json" -f $script:AkPython, (Join-Path $repoRoot 'kaizen.py'))
}
