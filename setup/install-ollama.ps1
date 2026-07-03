#Requires -Version 5.1
<#
  Light setup for the Ollama model backend (B* / model-*).

  Ollama installs via its own official installer (https://ollama.com/download); this script
  verifies it is present, relocates the model store under $DEVROOT (so weights stay out of the
  repo), pulls an embedding + chat model, and prints the env vars Kaizen reads. Run it yourself.

  Usage: setup\install-ollama.ps1 [-EmbedModel nomic-embed-text] [-ChatModel llama3.2] [-DevRoot D:\dev]
#>
[CmdletBinding()]
param(
    [string]$DevRoot,
    [string]$EmbedModel = 'nomic-embed-text',
    [string]$ChatModel = 'llama3.2'
)
$ErrorActionPreference = 'Stop'
function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "  ! $m" -ForegroundColor Yellow }

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $DevRoot) {
    if ($env:DEVROOT) { $DevRoot = $env:DEVROOT } else { $DevRoot = Split-Path -Parent $repoRoot }
}
$models = Join-Path $DevRoot 'Ollama\models'
New-Item -ItemType Directory -Force -Path $models | Out-Null

if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Warn 'Ollama is not on PATH. Install it from https://ollama.com/download, then re-run this script.'
    exit 1
}

Step "Model store: $models"
$env:OLLAMA_MODELS = $models
Step "Pulling embedding model: $EmbedModel"
& ollama pull $EmbedModel
if ($LASTEXITCODE -ne 0) { Warn 'embed-model pull failed (is the Ollama service running?).' }
Step "Pulling chat model: $ChatModel"
& ollama pull $ChatModel
if ($LASTEXITCODE -ne 0) { Warn 'chat-model pull failed (is the Ollama service running?).' }

Write-Host ''
Step 'Ollama backend ready. Point Kaizen at the models (current PowerShell session):'
Write-Host "  `$env:OLLAMA_MODELS     = '$models'"
Write-Host "  `$env:KAIZEN_EMBED_MODEL = '$EmbedModel'   # enables E3 embeddings + E4 --semantic"
Write-Host "  `$env:KAIZEN_LLM_MODEL   = '$ChatModel'    # enables B2 model-run"
Write-Host '  # persist for new shells with setx, e.g.:  setx KAIZEN_EMBED_MODEL nomic-embed-text'
Write-Host '  # remote OpenAI-compatible endpoint: set KAIZEN_EMBED_BASE_URL / KAIZEN_LLM_BASE_URL (+ *_API_KEY, env only)'
Write-Host '  Verify: python kaizen.py B1 --json'
