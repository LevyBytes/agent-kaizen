#Requires -Version 5.1
<#
  Guided install of ComfyUI as a DEVROOT sibling for Agent Kaizen's Y* backend.

  Usage:
    setup\install-comfyui.ps1 [-Gpu] [-CudaIndex url] [-Repo url] [-DevRoot D:\dev] [-PythonExe path] [-VenvPath path]
    setup\install-comfyui.ps1 -ListSteps|-PlanOnly|-SelfTest [-EmitPlanJson path]
    Safety/automation: -NoNetwork -NoExternalActions -NoUserEnvWrites -AssumeYes -NoInput -NoProgressHeader

  -Gpu selects CUDA torch; -CudaIndex is used only with -Gpu, defaults to the CUDA 12.1 wheel index, and must match the local driver/runtime.
  -Repo overrides the clone source. The first install clones the current unpinned default-branch HEAD; warm re-runs do not update it.
  -VenvPath places the venv outside the default <DevRoot>\ComfyUI\.venv. The installer does not set KAIZEN_COMFYUI_VENV; set it manually to the same path so Y6 finds the moved venv.
#>
[CmdletBinding()]
param(
    [string]$DevRoot,
    [string]$PythonExe,
    [string]$VenvPath,
    [switch]$Gpu,
    [string]$CudaIndex = 'https://download.pytorch.org/whl/cu121',
    [string]$Repo = 'https://github.com/comfyanonymous/ComfyUI.git',
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
    [pscustomobject]@{ Id='preflight'; Name='Resolve DEVROOT, Python, Git, and target paths' },
    [pscustomobject]@{ Id='clone'; Name='Clone or validate ComfyUI repository' },
    [pscustomobject]@{ Id='venv'; Name='Create ComfyUI virtual environment' },
    [pscustomobject]@{ Id='torch'; Name='Install CPU or CUDA torch packages' },
    [pscustomobject]@{ Id='deps'; Name='Install ComfyUI requirements' },
    [pscustomobject]@{ Id='summary'; Name='Print start and verification commands' }
)
Initialize-AkInstaller -Name 'Agent Kaizen ComfyUI installer' -RepoRoot $repoRoot -DevRoot $resolvedDevRoot -Steps $steps -PlanOnly:($PlanOnly -or $ListSteps) -NoProgressHeader:$NoProgressHeader -NoNetwork:$NoNetwork -NoExternalActions:$NoExternalActions -NoUserEnvWrites:$NoUserEnvWrites -AssumeYes:$AssumeYes -NoInput:$NoInput -SelfTest:$SelfTest

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
$script:AkGit = ''
$script:AkTarget = Join-Path $resolvedDevRoot 'ComfyUI'
$script:AkVenv = if ([string]::IsNullOrWhiteSpace($VenvPath)) { Join-Path $script:AkTarget '.venv' } else { $VenvPath }
$script:AkVenvPython = Join-Path $script:AkVenv 'Scripts\python.exe'

Invoke-AkStep -Id 'preflight' -Name 'Resolve DEVROOT, Python, Git, and target paths' -ScriptBlock {
    $script:AkPython = Resolve-AkPythonExe -PythonExe $PythonExe -DevRoot $resolvedDevRoot
    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) { throw 'git not found on PATH.' }
    $script:AkGit = $git.Source
    if (-not $Gpu -and (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
        Write-Host 'NVIDIA GPU detected. Re-run with -Gpu for a CUDA torch wheel; continuing with CPU torch.' -ForegroundColor Yellow
    } elseif ($Gpu -and -not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
        Write-Host 'CUDA torch requested, but nvidia-smi was not found; verify the NVIDIA driver before using ComfyUI.' -ForegroundColor Yellow
    }
    Write-Host ("DEVROOT: {0}" -f $resolvedDevRoot) -ForegroundColor Cyan
    Write-Host ("ComfyUI target: {0}" -f $script:AkTarget) -ForegroundColor Cyan
    Write-Host ("Python: {0}" -f $script:AkPython) -ForegroundColor Cyan
}

Invoke-AkStep -Id 'clone' -Name 'Clone or validate ComfyUI repository' -ScriptBlock {
    if (Test-Path -LiteralPath (Join-Path $script:AkTarget 'main.py')) {
        Write-Host ("ComfyUI already present: {0}" -f $script:AkTarget) -ForegroundColor Green
    } else {
        Assert-AkNetworkAllowed $Repo
        Invoke-AkNative -Exe $script:AkGit -Arguments @('clone','--depth','1',$Repo,$script:AkTarget) -ActivityNote 'Git is cloning ComfyUI repository data; network speed controls the duration.'
    }
}

Invoke-AkStep -Id 'venv' -Name 'Create ComfyUI virtual environment' -ScriptBlock {
    if (Test-Path -LiteralPath $script:AkVenvPython) {
        Write-Host ("ComfyUI venv already exists: {0}" -f $script:AkVenv) -ForegroundColor Green
    } else {
        Invoke-AkNative -Exe $script:AkPython -Arguments @('-m','venv',$script:AkVenv) -ActivityNote 'Creating the ComfyUI Python virtual environment.'
        Assert-AkNetworkAllowed 'pip upgrade in ComfyUI venv'
        Invoke-AkNative -Exe $script:AkVenvPython -Arguments @('-m','pip','install','--upgrade','pip') -ActivityNote 'Upgrading pip inside the ComfyUI venv.'
    }
}

Invoke-AkStep -Id 'torch' -Name 'Install CPU or CUDA torch packages' -ScriptBlock {
    Assert-AkNetworkAllowed 'torch package installation'
    if ($Gpu) {
        Invoke-AkNative -Exe $script:AkVenvPython -Arguments @('-m','pip','install','torch','torchvision','torchaudio','--index-url',$CudaIndex) -ActivityNote ('Installing CUDA torch packages from {0}.' -f $CudaIndex)
    } else {
        Invoke-AkNative -Exe $script:AkVenvPython -Arguments @('-m','pip','install','torch','torchvision','torchaudio') -ActivityNote 'Installing CPU torch packages.'
    }
}

Invoke-AkStep -Id 'deps' -Name 'Install ComfyUI requirements' -ScriptBlock {
    $req = Join-Path $script:AkTarget 'requirements.txt'
    if (-not (Test-Path -LiteralPath $req)) { throw "ComfyUI requirements file not found: $req" }
    Assert-AkNetworkAllowed 'ComfyUI requirements installation'
    Invoke-AkNative -Exe $script:AkVenvPython -Arguments @('-m','pip','install','-r',$req) -ActivityNote 'Installing ComfyUI requirements; pip output is logged and tailed while this runs.'
}

Invoke-AkStep -Id 'summary' -Name 'Print start and verification commands' -ScriptBlock {
    Write-Host ''
    Write-Host 'ComfyUI installed.' -ForegroundColor Green
    Write-Host ("  Models:    place checkpoints under {0}" -f (Join-Path $script:AkTarget 'models\checkpoints'))
    Write-Host ("  Start:     & '{0}' '{1}'" -f $script:AkVenvPython, (Join-Path $script:AkTarget 'main.py'))
    Write-Host '  URL:       http://127.0.0.1:8188  (override with KAIZEN_COMFYUI_URL or --endpoint)'
    Write-Host '  Verify:    python kaizen.py Y5 --json'
}
