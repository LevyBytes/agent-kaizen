#Requires -Version 5.1
<#
.SYNOPSIS
Light setup for the Ollama model backend (B* / model-*).
.DESCRIPTION
Pulls the selected models and prints the process-scoped environment needed by Agent Kaizen. If an Ollama server is already running, that server keeps the model-store path it inherited at startup; restart it with OLLAMA_MODELS set before relying on DEVROOT relocation.
.PARAMETER DevRoot
DEVROOT containing the external Ollama model store.
.PARAMETER EmbedModel
Embedding model identifier passed to ollama pull.
.PARAMETER ChatModel
Chat model identifier passed to ollama pull.
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
Block model pulls.
.PARAMETER NoExternalActions
Block directory creation and native commands.
.PARAMETER NoUserEnvWrites
Shared installer selector; this script sets process environment only and never persists user variables.
.PARAMETER AssumeYes
Shared non-interactive installer selector; this script has no prompt.
.PARAMETER NoInput
Prevent interactive DEVROOT selection.
.EXAMPLE
setup\install-ollama.ps1 -PlanOnly -NoNetwork -NoExternalActions -NoUserEnvWrites -NoInput
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
if ($SelfTest) {
    Show-AkPlan
    if (-not [string]::IsNullOrWhiteSpace($EmitPlanJson)) { Write-AkPlanJson -Path $EmitPlanJson }
}

$script:AkOllama = ''
$script:AkModels = Join-Path $resolvedDevRoot 'Ollama\models'
$script:AkServerWasRunning = $false

Invoke-AkStep -Id 'preflight' -Name 'Resolve DEVROOT, model store, and ollama command' -ScriptBlock {
    if (-not (Test-Path -LiteralPath $script:AkModels)) { Assert-AkExternalAllowed ("create model store {0}" -f $script:AkModels) }
    New-AkDirectory $script:AkModels
    $env:OLLAMA_MODELS = $script:AkModels
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $cmd) { throw 'Ollama is not on PATH. Install it from https://ollama.com/download, then re-run this script.' }
    $script:AkOllama = $cmd.Source
    $script:AkServerWasRunning = $null -ne (Get-Process -Name 'ollama' -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($script:AkServerWasRunning) { Write-Host 'Ollama is already running; restart that server with OLLAMA_MODELS set before expecting the DEVROOT model store.' -ForegroundColor Yellow }
    Write-Host ("Model store: {0}" -f $script:AkModels) -ForegroundColor Cyan
    Write-Host ("Ollama: {0}" -f $script:AkOllama) -ForegroundColor Cyan
}

Invoke-AkStep -Id 'embed' -Name 'Pull embedding model' -ScriptBlock {
    Assert-AkNetworkAllowed $EmbedModel
    Invoke-AkNative -Exe $script:AkOllama -Arguments @('pull',$EmbedModel) -ActivityNote ("Ollama is downloading/verifying model data for {0}; progress comes from ollama output when available." -f $EmbedModel)
}

Invoke-AkStep -Id 'chat' -Name 'Pull chat model' -ScriptBlock {
    Assert-AkNetworkAllowed $ChatModel
    Invoke-AkNative -Exe $script:AkOllama -Arguments @('pull',$ChatModel) -ActivityNote ("Ollama is downloading/verifying model data for {0}; progress comes from ollama output when available." -f $ChatModel)
}

Invoke-AkStep -Id 'summary' -Name 'Print backend environment and verification command' -ScriptBlock {
    Write-Host ''
    Write-Host 'Ollama backend ready. Recommended settings (only OLLAMA_MODELS was applied to this process):' -ForegroundColor Green
    Write-Host ("  `$env:OLLAMA_MODELS      = '{0}'" -f $script:AkModels)
    Write-Host ("  `$env:KAIZEN_EMBED_MODEL = '{0}'" -f $EmbedModel)
    Write-Host ("  `$env:KAIZEN_LLM_MODEL   = '{0}'" -f $ChatModel)
    Write-Host '  Model-store relocation applies to pulls only when this process starts the server; restart an existing server with OLLAMA_MODELS set.'
    Write-Host '  Remote OpenAI-compatible endpoint: set KAIZEN_EMBED_BASE_URL / KAIZEN_LLM_BASE_URL and API keys in env only.'
    Write-Host '  Verify: python kaizen.py B1 --json'
}
