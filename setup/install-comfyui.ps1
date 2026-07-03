#Requires -Version 5.1
<#
  Guided install of ComfyUI as a $DEVROOT sibling, for Agent Kaizen's Y* (comfy-*) backend.

  Clones ComfyUI OUTSIDE the agent-kaizen repo (so multi-GB weights are never tracked by git),
  creates a venv, installs requirements with a CPU/GPU branch, and prints the start command.
  Model weights are NOT auto-downloaded (licensing + size). Run this yourself; Kaizen only needs
  the endpoint URL (default http://127.0.0.1:8188).

  Usage:
    setup\install-comfyui.ps1                 # CPU torch
    setup\install-comfyui.ps1 -Gpu            # CUDA torch wheel
    setup\install-comfyui.ps1 -DevRoot D:\dev # explicit sibling root
#>
[CmdletBinding()]
param(
    [string]$DevRoot,
    [switch]$Gpu,
    [string]$CudaIndex = 'https://download.pytorch.org/whl/cu121',
    [string]$Repo = 'https://github.com/comfyanonymous/ComfyUI.git'
)
$ErrorActionPreference = 'Stop'
function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "  ! $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

# agent-kaizen repo root = parent of this setup/ dir; $DEVROOT = its parent (the sibling convention).
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $DevRoot) {
    if ($env:DEVROOT) { $DevRoot = $env:DEVROOT } else { $DevRoot = Split-Path -Parent $repoRoot }
}
$target = Join-Path $DevRoot 'ComfyUI'
Step "DEVROOT:        $DevRoot"
Step "ComfyUI target: $target"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die 'git not found on PATH.' }
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) { $pyCmd = Get-Command py -ErrorAction SilentlyContinue }
if (-not $pyCmd) { Die 'python not found on PATH.' }
$python = $pyCmd.Source

if (-not $Gpu -and (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    Warn 'NVIDIA GPU detected. Re-run with -Gpu for a CUDA torch wheel; continuing with CPU torch.'
}

if (Test-Path $target) {
    Step "ComfyUI already present (skipping clone). Pull updates yourself if desired."
} else {
    Step 'Cloning ComfyUI...'
    & git clone --depth 1 $Repo $target
    if ($LASTEXITCODE -ne 0) { Die 'git clone failed.' }
}

$venv = Join-Path $target '.venv'
$venvPy = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Step "Creating venv at $venv ..."
    & $python -m venv $venv
    if ($LASTEXITCODE -ne 0) { Die 'venv creation failed.' }
}
& $venvPy -m pip install --upgrade pip | Out-Null

if ($Gpu) {
    Step "Installing CUDA torch from $CudaIndex ..."
    & $venvPy -m pip install torch torchvision torchaudio --index-url $CudaIndex
} else {
    Step 'Installing CPU torch ...'
    & $venvPy -m pip install torch torchvision torchaudio
}
if ($LASTEXITCODE -ne 0) { Die 'torch install failed.' }
Step 'Installing ComfyUI requirements ...'
& $venvPy -m pip install -r (Join-Path $target 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Die 'ComfyUI requirements install failed.' }

Write-Host ''
Step 'ComfyUI installed.'
Write-Host "  Models:    place a checkpoint under $target\models\checkpoints\  (not auto-downloaded)"
Write-Host "  Start:     & `"$venvPy`" `"$(Join-Path $target 'main.py')`""
Write-Host '  URL:       http://127.0.0.1:8188  (override with KAIZEN_COMFYUI_URL or --endpoint)'
Write-Host '  Verify:    python kaizen.py Y5 --json'
Write-Host "  Workspace: add $target to your local .code-workspace to see it in the Explorer (stays out of git)"
