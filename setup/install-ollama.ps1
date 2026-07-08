#Requires -Version 5.1
<#
  Light setup for the Ollama model backend (B* / model-*).

  Usage:
    setup\install-ollama.ps1 [-EmbedModel <name>] [-ChatModel <name>] [-DevRoot D:\dev]
    setup\install-ollama.ps1 -ListSteps
    setup\install-ollama.ps1 -PlanOnly -NoExternalActions
#>
[CmdletBinding()]
param(
    [string]$DevRoot,
    [string]$EmbedModel = 'hf.co/mradermacher/KaLM-embedding-multilingual-mini-instruct-v2.5-GGUF:Q8_0',
    [string]$ChatModel = 'hf.co/unsloth/Qwen3.5-9B-GGUF:Q4_K_M',
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
    [pscustomobject]@{ Id='preflight'; Name='Resolve DEVROOT, model store, and ollama command' },
    [pscustomobject]@{ Id='embed'; Name='Pull embedding model' },
    [pscustomobject]@{ Id='chat'; Name='Pull chat model' },
    [pscustomobject]@{ Id='summary'; Name='Print backend environment and verification command' }
)
Initialize-AkInstaller -Name 'Agent Kaizen Ollama backend installer' -RepoRoot $repoRoot -DevRoot $resolvedDevRoot -Steps $steps -PlanOnly:($PlanOnly -or $ListSteps) -NoProgressHeader:$NoProgressHeader -NoNetwork:$NoNetwork -NoExternalActions:$NoExternalActions -NoUserEnvWrites:$NoUserEnvWrites -AssumeYes:$AssumeYes -NoInput:$NoInput -SelfTest:$SelfTest

if ($ListSteps -or $PlanOnly) {
    Show-AkPlan
    if (-not [string]::IsNullOrWhiteSpace($EmitPlanJson)) { Write-AkPlanJson -Path $EmitPlanJson }
    exit 0
}
if ($SelfTest) { Show-AkPlan }

$script:AkOllama = ''
$script:AkModels = Join-Path $resolvedDevRoot 'Ollama\models'

Invoke-AkStep -Id 'preflight' -Name 'Resolve DEVROOT, model store, and ollama command' -ScriptBlock {
    New-AkDirectory $script:AkModels
    $env:OLLAMA_MODELS = $script:AkModels
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $cmd) { throw 'Ollama is not on PATH. Install it from https://ollama.com/download, then re-run this script.' }
    $script:AkOllama = $cmd.Source
    Write-Host ("Model store: {0}" -f $script:AkModels) -ForegroundColor Cyan
    Write-Host ("Ollama: {0}" -f $script:AkOllama) -ForegroundColor Cyan
}

Invoke-AkStep -Id 'embed' -Name 'Pull embedding model' -ScriptBlock {
    Invoke-AkNative -Exe $script:AkOllama -Arguments @('pull',$EmbedModel) -ActivityNote ("Ollama is downloading/verifying model data for {0}; progress comes from ollama output when available." -f $EmbedModel)
}

Invoke-AkStep -Id 'chat' -Name 'Pull chat model' -ScriptBlock {
    Invoke-AkNative -Exe $script:AkOllama -Arguments @('pull',$ChatModel) -ActivityNote ("Ollama is downloading/verifying model data for {0}; progress comes from ollama output when available." -f $ChatModel)
}

Invoke-AkStep -Id 'summary' -Name 'Print backend environment and verification command' -ScriptBlock {
    Write-Host ''
    Write-Host 'Ollama backend ready. Current-session settings:' -ForegroundColor Green
    Write-Host ("  `$env:OLLAMA_MODELS      = '{0}'" -f $script:AkModels)
    Write-Host ("  `$env:KAIZEN_EMBED_MODEL = '{0}'" -f $EmbedModel)
    Write-Host ("  `$env:KAIZEN_LLM_MODEL   = '{0}'" -f $ChatModel)
    Write-Host '  Remote OpenAI-compatible endpoint: set KAIZEN_EMBED_BASE_URL / KAIZEN_LLM_BASE_URL and API keys in env only.'
    Write-Host '  Verify: python kaizen.py B1 --json'
}
