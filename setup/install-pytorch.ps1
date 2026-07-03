#Requires -Version 5.1
<#
  Install the OPT-IN PyTorch extra (in-process sentence-transformers embeddings + semantic chunking).

  Embeddings run IN-PROCESS, so this installs into the python on PATH -- run it with the SAME python
  (venv) you launch kaizen.py with. Redirects the HuggingFace weight cache to $DEVROOT/models via
  HF_HOME so weights stay out of the repo. torch is heavy + GPU-specific (use -Gpu for a CUDA wheel).

  Usage: setup\install-pytorch.ps1 [-Gpu] [-CudaIndex URL] [-DevRoot D:\dev]
#>
[CmdletBinding()]
param(
    [string]$DevRoot,
    [switch]$Gpu,
    [string]$CudaIndex = 'https://download.pytorch.org/whl/cu121'
)
$ErrorActionPreference = 'Stop'
function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "  ! $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $DevRoot) {
    if ($env:DEVROOT) { $DevRoot = $env:DEVROOT } else { $DevRoot = Split-Path -Parent $repoRoot }
}
$cache = Join-Path $DevRoot 'models'
New-Item -ItemType Directory -Force -Path $cache | Out-Null
$env:HF_HOME = $cache

$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) { $pyCmd = Get-Command py -ErrorAction SilentlyContinue }
if (-not $pyCmd) { Die 'python not found on PATH.' }
$python = $pyCmd.Source
Step "Installing into: $python  (must be the venv you run kaizen.py with)"
Step "HF weight cache: $cache  (HF_HOME)"

if ($Gpu) {
    Step "Installing CUDA torch from $CudaIndex ..."
    & $python -m pip install torch --index-url $CudaIndex
    if ($LASTEXITCODE -ne 0) { Die 'torch (CUDA) install failed.' }
}
Step 'Installing the opt-in extra (sentence-transformers + torch) ...'
& $python -m pip install -r (Join-Path $repoRoot 'requirements-pytorch.txt')
if ($LASTEXITCODE -ne 0) { Die 'requirements-pytorch.txt install failed.' }

Write-Host ''
Step 'PyTorch embedding backend installed. Point Kaizen at it (current session):'
Write-Host "  `$env:HF_HOME              = '$cache'"
Write-Host "  `$env:KAIZEN_EMBED_BACKEND = 'sentence-transformers'"
Write-Host "  `$env:KAIZEN_EMBED_MODEL   = 'all-MiniLM-L6-v2'   # optional; this is the default"
Write-Host '  Verify: python kaizen.py B1 --json'
