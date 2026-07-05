@echo off
REM ============================================================================
REM  Install-Agent-Kaizen.cmd -- self-contained Windows installer (thin launcher)
REM ----------------------------------------------------------------------------
REM  Download this ONE file and double-click it, or run it from a terminal. It
REM  extracts the embedded PowerShell installer and relaunches it in an ELEVATED
REM  PowerShell console (via a UAC prompt). That PowerShell window chooses the
REM  install root, then bootstraps winget, Git, Python, the shared venv, the
REM  Rust/Build Tools toolchain, dependencies, the DB, and workspace files.
REM  Re-running is safe; existing pieces are detected and reused.
REM
REM  Usage:
REM    Install-Agent-Kaizen.cmd [DEVROOT] [-Ref <git-tag>] [-NoPrompt] [-NoPause]
REM    Install-Agent-Kaizen.cmd X:\dev -NoPause
REM    Install-Agent-Kaizen.cmd X:\dev -PlanOnly -NoNetwork -NoExternalActions
REM
REM  Automation flags (-SelfTest / -PlanOnly / -ListSteps / -NoPrompt /
REM  -NoNetwork / -NoExternalActions) run the payload inline in the current
REM  console -- no new window, no UAC -- so CI and self-test stay headless.
REM
REM  Public repo: https://github.com/LevyBytes/agent-kaizen
REM ============================================================================
setlocal EnableExtensions DisableDelayedExpansion
title Agent Kaizen Setup

set "AK_SELF=%~f0"
set "AK_EXIT=0"
set "AK_INLINE=0"
set "AK_PASSTHRU=%*"

where powershell.exe >nul 2>nul
if errorlevel 1 (
  echo Windows PowerShell 5.1+ is required but was not found on PATH.
  echo It ships with Windows 10 and 11. Install/enable it, then run this again.
  pause
  exit /b 1
)

REM Non-interactive / automation modes run inline (this console, no elevation,
REM no new window) so CI, self-test, and plan-only stay headless.
for %%A in (%*) do (
  if /I "%%~A"=="-SelfTest" set "AK_INLINE=1"
  if /I "%%~A"=="-ListSteps" set "AK_INLINE=1"
  if /I "%%~A"=="-PlanOnly" set "AK_INLINE=1"
  if /I "%%~A"=="-NoPrompt" set "AK_INLINE=1"
  if /I "%%~A"=="-NoNetwork" set "AK_INLINE=1"
  if /I "%%~A"=="-NoExternalActions" set "AK_INLINE=1"
)

echo Preparing Agent Kaizen setup...
call :ExtractPayload
if errorlevel 1 (
  echo.
  echo Could not extract the embedded PowerShell payload.
  endlocal & exit /b 9
)

if "%AK_INLINE%"=="1" goto :RunInline
goto :RunElevated

:RunInline
REM Headless/automation: run the payload directly in this console.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%AK_PAYLOAD%" %*
set "AK_EXIT=%ERRORLEVEL%"
del "%AK_PAYLOAD%" >nul 2>nul
endlocal & exit /b %AK_EXIT%

:RunElevated
REM Interactive install: relaunch the payload in an ELEVATED PowerShell console
REM window via UAC. That window owns the DEVROOT menu, every install step, and
REM the closing pause; the payload deletes its own temp file when it finishes.
REM This launcher window closes as soon as the elevated window starts.
echo Launching Agent Kaizen setup in an elevated PowerShell window...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$q=[char]34; $a='-NoProfile -ExecutionPolicy Bypass -File '+$q+$env:AK_PAYLOAD+$q; if(-not [string]::IsNullOrWhiteSpace($env:AK_PASSTHRU)){ $a=$a+' '+$env:AK_PASSTHRU }; Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $a"
if errorlevel 1 (
  echo.
  echo Could not start the elevated PowerShell installer. If a User Account
  echo Control prompt appeared and was declined, rerun and choose Yes.
  endlocal & exit /b 1
)
endlocal & exit /b 0

:ExtractPayload
set "AK_PAYLOAD=%TEMP%\Agent-Kaizen-Setup-%RANDOM%%RANDOM%.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $self=$env:AK_SELF; $out=$env:AK_PAYLOAD; $marker='__AK_PS_PAYLOAD__'; $raw=[IO.File]::ReadAllText($self,[Text.Encoding]::UTF8); $idx=$raw.LastIndexOf($marker); if($idx -lt 0){ throw 'Installer payload marker not found.' }; $payload=$raw.Substring($idx+$marker.Length).TrimStart([char[]]@([char]13,[char]10)); $utf8=New-Object System.Text.UTF8Encoding($false); [IO.File]::WriteAllText($out,$payload,$utf8)"
exit /b %ERRORLEVEL%

__AK_PS_PAYLOAD__
param(
    [Parameter(Position = 0)]
    [string] $DevRoot,
    [string] $RepoName = 'agent-kaizen',
    [string] $RepoUrl = 'https://github.com/LevyBytes/agent-kaizen.git',
    [string] $RepoSource,
    [string] $Ref,
    [switch] $NoPersistDevRoot,
    [switch] $NoPrompt,
    [switch] $NoPause,
    [switch] $PlanOnly,
    [switch] $ListSteps,
    [string] $EmitPlanJson,
    [switch] $SelfTest,
    [switch] $NoProgressHeader,
    [switch] $NoNetwork,
    [switch] $NoExternalActions,
    [switch] $NoUserEnvWrites,
    [switch] $AssumeYes,
    [switch] $WithRust,
    [switch] $WithBuildTools,
    [switch] $WithDotNet,
    [switch] $WithCMake,
    [switch] $WithNode,
    [switch] $WithVSCode,
    [switch] $NoDevTools,
    [string] $PyTursoWheelUrl
)

$ErrorActionPreference = 'Stop'
# Suppress the built-in Write-Progress banner globally. Add-AppxPackage (winget/appx bootstrap) and
# Expand-Archive (CMake/VS Code/Node) otherwise render a flashing, offset, stale-record sticky banner
# (an epilepsy hazard). The installer's own [n/N] Write-Host step + download output is unaffected.
$ProgressPreference = 'SilentlyContinue'
# The cmd launcher relaunches this payload in an elevated PowerShell window; title it so the
# elevated console reads "Administrator: Agent Kaizen Setup" instead of a bare PowerShell prompt.
try { $Host.UI.RawUI.WindowTitle = 'Agent Kaizen Setup' } catch {}
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

$WorkspaceJson = @'
{
  "folders": [
    {
      "name": "Agent Kaizen",
      "path": ".."
    },
    {
      "name": "SKILLS",
      "path": "../../SKILLS"
    }
  ],
  "settings": {
    "terminal.integrated.cwd": "${workspaceFolder:Agent Kaizen}",
    "terminal.integrated.defaultProfile.windows": "Agent Kaizen Developer PowerShell",
    "terminal.integrated.profiles.windows": {
      "Agent Kaizen Developer PowerShell": {
        "path": "powershell.exe",
        "args": [
          "-NoExit",
          "-ExecutionPolicy",
          "Bypass",
          "-Command",
          "__AK_DEVSHELL_PREFIX__$projectRoot=(Resolve-Path '${workspaceFolder:Agent Kaizen}').Path; $pythonActivate='${workspaceFolder:Agent Kaizen}/../Python/venvs/kaizen/Scripts/Activate.ps1'; if (Test-Path $pythonActivate) { & $pythonActivate } else { Write-Warning 'Python venv not found. Run the setup script.' }; Set-Location $projectRoot"
        ]
      },
      "PowerShell": {
        "source": "PowerShell"
      }
    },
    "terminal.integrated.env.windows": {
      "AGENT_KAIZEN_ROOT": "${workspaceFolder:Agent Kaizen}",
      "AGENT_KAIZEN_VENV": "${workspaceFolder:Agent Kaizen}/../Python/venvs/kaizen"
    },
    "python.defaultInterpreterPath": "${workspaceFolder:Agent Kaizen}/../Python/venvs/kaizen/Scripts/python.exe",
    "python.terminal.activateEnvironment": false,
    "files.exclude": {
      "**/__pycache__": true
    },
    "search.exclude": {
      "**/__pycache__": true
    }
  }
}
'@

$LauncherCmd = @'
@echo off
setlocal

set "AGENT_KAIZEN_ROOT=%~dp0"
cd /d "%AGENT_KAIZEN_ROOT%"

code "%AGENT_KAIZEN_ROOT%_workspace\agent-kaizen-tools.code-workspace"

endlocal
'@

$script:Steps = @(
    [pscustomobject]@{ Id = 'devroot'; Name = 'Resolve DEVROOT and create setup folders' },
    [pscustomobject]@{ Id = 'preflight'; Name = 'Preflight Windows Sandbox, source, and safety mode' },
    [pscustomobject]@{ Id = 'toolselect'; Name = 'Choose developer tools to install' },
    [pscustomobject]@{ Id = 'winget'; Name = 'Detect winget (optional; Git/Python fall back to direct downloads)' },
    [pscustomobject]@{ Id = 'git'; Name = 'Install or validate Git' },
    [pscustomobject]@{ Id = 'python'; Name = 'Install or validate Python 3.12+' },
    [pscustomobject]@{ Id = 'rust'; Name = 'Install Rust (needed to compile Turso)' },
    [pscustomobject]@{ Id = 'buildtools'; Name = 'Install Visual Studio Build Tools (needed to compile Turso)' },
    [pscustomobject]@{ Id = 'repo'; Name = 'Clone or update Agent Kaizen repository' },
    [pscustomobject]@{ Id = 'venv'; Name = 'Create or refresh shared Python venv' },
    [pscustomobject]@{ Id = 'deps'; Name = 'Install Agent Kaizen Python dependencies' },
    [pscustomobject]@{ Id = 'workspace'; Name = 'Generate workspace, launcher, and skills folder' },
    [pscustomobject]@{ Id = 'skills'; Name = 'Select and install Agent Kaizen skills' },
    [pscustomobject]@{ Id = 'health'; Name = 'Initialize DB and run health checks' },
    [pscustomobject]@{ Id = 'dotnet'; Name = 'Optional: install .NET SDKs (8 and 9)' },
    [pscustomobject]@{ Id = 'cmake'; Name = 'Optional: install CMake' },
    [pscustomobject]@{ Id = 'node'; Name = 'Optional: install Node.js' },
    [pscustomobject]@{ Id = 'vscode'; Name = 'Optional: install VS Code' }
)

$script:StepRecords = New-Object System.Collections.ArrayList
$script:CurrentStep = 0
$script:CommandCounter = 0
# User-skipped-step signalling (set by Set-AkStepSkipped; read by Invoke-AkStep).
$script:StepSkipped = $false
$script:StepSkipReason = ''
# Sticky progress banner state (reserved-block cursor rewrite; see Initialize-AkBanner/Write-AkBanner).
$script:BannerEnabled = $false
$script:BannerTop = 0
$script:BannerHeight = 6
$script:BannerCompleted = 0
$script:BannerStepNo = 0
$script:BannerStatus = 'working'
$script:BannerName = ''
$script:BannerActivity = ''
$script:BannerLastPaintTicks = 0
$script:BannerLastActivityPct = -1
$script:DevRoot = $null
$script:RepoPath = $null
$script:VenvRoot = $null
$script:VenvPython = $null
$script:PythonBaseDir = $null
$script:LogRoot = $null
$script:StatePath = $null
$script:WorkRoot = $null
$script:Winget = $null
$script:Python = $null
$script:Git = $null
$script:WindowsAppRuntimeInstallerPath = $null
$script:LastCommandLogPath = $null
$script:LastCommandLine = $null
$script:LastActivityNote = $null
$script:TranscriptLogPath = $null
$script:TranscriptStarted = $false
$script:SupportBundlePath = $null
$script:LastAppxActivityId = $null
$script:WingetBootstrapAttempted = $false
$script:WingetUnavailable = $false
$script:SmartAppControl = $null
# Optional dev-toolchain roots under DEVROOT (set in Initialize-AkContext).
$script:RustRoot = $null
$script:VSCodeRoot = $null
$script:DotNetRoot = $null
$script:CMakeRoot = $null
$script:NodeRoot = $null
$script:NpmGlobalDir = $null
$script:WheelCache = $null
$script:VsDevShell = $null
$script:VsInstallPath = $null
$script:VsDevShellModule = $null
# Which optional tools to install (resolved by Select-AkDevTools). Rust + Build Tools are needed
# to compile pyturso (no Windows wheel); the rest are optional dev conveniences.
$script:ToolRust = $false
$script:ToolBuildTools = $false
$script:ToolDotNet = $false
$script:ToolCMake = $false
$script:ToolNode = $false
$script:ToolVSCode = $false

# Pinned fallback sources, used only when the live "latest" lookup fails.
# Bump path: refresh these after confirming the release assets exist.
#   Git for Windows -> https://github.com/git-for-windows/git/releases (asset Git-*-64-bit.exe / Git-*-arm64.exe)
#   Python          -> python.org Windows installer (last 3.12.x with a Windows binary installer)
#   Dev toolchain   -> Rust (winget Rustlang.Rustup), VS Build Tools (aka.ms/vs/17), VS Code archive, CMake (Kitware), Node (nodejs.org), .NET (dot.net)
$script:PinnedGitInstallerUri = 'https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.2/Git-2.55.0.2-64-bit.exe'
$script:PinnedGitInstallerUriArm64 = 'https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.2/Git-2.55.0.2-arm64.exe'
$script:PinnedPythonVersion = '3.12.10'
$script:PinnedRustupInitUri = 'https://win.rustup.rs/x86_64'
$script:PinnedVSBuildToolsUri = 'https://aka.ms/vs/17/release/vs_BuildTools.exe'
$script:PinnedVSCodeArchiveUri = 'https://update.code.visualstudio.com/latest/win32-x64-archive/stable'
$script:PinnedDotNetInstallScriptUri = 'https://dot.net/v1/dotnet-install.ps1'
$script:PinnedCMakeVersion = '3.31.6'
$script:PinnedNodeVersion = '22.21.1'

function Write-Info { param([string] $Message) Write-Host "      $Message" }
function Write-Ok { param([string] $Message) Write-Host "      [ok] $Message" -ForegroundColor Green }
function Write-Warn { param([string] $Message) Write-Host "      [warn] $Message" -ForegroundColor Yellow }

function Write-Utf8NoBom {
    param([Parameter(Mandatory)][string] $Path, [Parameter(Mandatory)][string] $Content)
    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $enc = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Content, $enc)
}

function Write-Utf8IfChanged {
    # Verify pillar: only write when the content actually differs, so a warm re-run does not churn
    # files (or trigger editor/watch events) for identical output.
    param([Parameter(Mandatory)][string] $Path, [Parameter(Mandatory)][string] $Content)
    if (Test-Path -LiteralPath $Path) {
        try { if ([IO.File]::ReadAllText($Path, (New-Object System.Text.UTF8Encoding($false))) -ceq $Content) { return } } catch {}
    }
    Write-Utf8NoBom -Path $Path -Content $Content
}

function Add-AkSupportLine {
    param([string] $Message = '')
    if ([string]::IsNullOrWhiteSpace($script:SupportBundlePath)) { return }
    try {
        $parent = Split-Path -Parent $script:SupportBundlePath
        if ($parent -and -not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        Add-Content -LiteralPath $script:SupportBundlePath -Value $Message -Encoding UTF8
    } catch {}
}

function Add-AkSupportSection {
    param([Parameter(Mandatory)][string] $Title)
    Add-AkSupportLine ''
    Add-AkSupportLine ("=== {0} ===" -f $Title)
}

function Start-AkTranscriptAndEnvironment {
    if ([string]::IsNullOrWhiteSpace($script:LogRoot)) { return }
    if (-not (Test-Path -LiteralPath $script:LogRoot)) {
        New-Item -ItemType Directory -Path $script:LogRoot -Force | Out-Null
    }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $script:TranscriptLogPath = Join-Path $script:LogRoot ("setup-$stamp-transcript.log")
    $script:SupportBundlePath = Join-Path $script:LogRoot ("support-$stamp.txt")
    Write-Utf8NoBom -Path $script:SupportBundlePath -Content ("Agent Kaizen setup support bundle`r`nCreated: {0}`r`nDEVROOT: {1}`r`nLogs: {2}`r`n" -f (Get-Date).ToString('s'), $script:DevRoot, $script:LogRoot)
    Add-AkSupportLine ("PowerShell: {0}" -f $PSVersionTable.PSVersion)
    Add-AkSupportLine ("Admin: {0}" -f (Test-IsAdmin))
    Add-AkSupportLine ("Computer: {0}" -f $env:COMPUTERNAME)
    Add-AkSupportLine ("Windows Sandbox: {0}" -f (Test-WindowsSandbox))
    try {
        Start-Transcript -Path $script:TranscriptLogPath -Append | Out-Null
        $script:TranscriptStarted = $true
        Write-Host ("Transcript log: {0}" -f $script:TranscriptLogPath)
        Write-Host ("Support bundle: {0}" -f $script:SupportBundlePath)
    } catch {
        Add-AkSupportLine ("Transcript start failed: {0}" -f $_.Exception.Message)
        Write-Warn "Transcript could not start: $($_.Exception.Message)"
    }
}

function Stop-AkTranscript {
    if (-not $script:TranscriptStarted) { return }
    try { Stop-Transcript | Out-Null } catch {}
    $script:TranscriptStarted = $false
}

function Copy-AkLogMirror {
    # Copy the setup logs to a writable location that survives the run. In Windows Sandbox the
    # setup folder is ephemeral, so a host-mapped folder (via AK_LOG_MIRROR, or C:\ak-logs) is the
    # only way to keep the transcript/support bundle after the sandbox closes.
    if ($PlanOnly -or $SelfTest -or $ListSteps) { return }
    # Driven solely by the AK_LOG_MIRROR environment variable (the .wsb sets it). No hardcoded path.
    $mirror = $env:AK_LOG_MIRROR
    if ([string]::IsNullOrWhiteSpace($mirror)) { return }
    if ([string]::IsNullOrWhiteSpace($script:LogRoot) -or -not (Test-Path -LiteralPath $script:LogRoot)) { return }
    try {
        $dest = Join-Path $mirror ("setup-logs-{0}-{1}" -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $PID)
        New-Item -ItemType Directory -Path $dest -Force | Out-Null
        Copy-Item -Path (Join-Path $script:LogRoot '*') -Destination $dest -Recurse -Force -ErrorAction SilentlyContinue
        if (-not [string]::IsNullOrWhiteSpace($script:StatePath) -and (Test-Path -LiteralPath $script:StatePath)) {
            Copy-Item -LiteralPath $script:StatePath -Destination $dest -Force -ErrorAction SilentlyContinue
        }
        Write-Host ("Log mirror: {0}" -f $dest)
    } catch {
        Write-Warn "Log mirror failed: $($_.Exception.Message)"
    }
}

function Get-AkPackageInventoryLines {
    $names = @('Microsoft.WindowsAppRuntime.1.8', 'Microsoft.DesktopAppInstaller', 'Microsoft.Winget.Source')
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($name in $names) {
        $packages = @()
        try {
            $packages = @(Get-AppxPackage -Name $name -AllUsers -ErrorAction SilentlyContinue)
        } catch {
            try { $packages = @(Get-AppxPackage -Name $name -ErrorAction SilentlyContinue) } catch { $packages = @() }
        }
        if ($packages.Count -eq 0) {
            [void]$lines.Add(("{0}: not found" -f $name))
        } else {
            foreach ($pkg in $packages) {
                [void]$lines.Add(("{0}: version={1} arch={2} full={3} location={4}" -f $name, [string]$pkg.Version, [string]$pkg.Architecture, [string]$pkg.PackageFullName, [string]$pkg.InstallLocation))
            }
        }
    }
    return @($lines.ToArray())
}

function Write-AkPackageInventory {
    param([Parameter(Mandatory)][string] $Label)
    Add-AkSupportSection ("Package inventory - $Label")
    foreach ($line in (Get-AkPackageInventoryLines)) {
        Add-AkSupportLine $line
    }
}

function Get-AkActivityIdFromText {
    param([string] $Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $guid = '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
    if ($Text -match ("(?i)ActivityId[^\r\n]*?(" + $guid + ")")) { return [string]$matches[1] }
    if ($Text -match $guid) { return [string]$matches[0] }
    return $null
}

function Add-AkAppxFailureEvidence {
    param([Parameter(Mandatory)] $ErrorRecord)
    $message = [string]$ErrorRecord.Exception.Message
    Add-AkSupportSection 'AppX failure'
    Add-AkSupportLine $message
    $activityId = Get-AkActivityIdFromText -Text $message
    if ([string]::IsNullOrWhiteSpace($activityId)) { return }
    $script:LastAppxActivityId = $activityId
    Add-AkSupportLine ("ActivityId: {0}" -f $activityId)
    try {
        $cmd = Get-Command Get-AppPackageLog -ErrorAction SilentlyContinue
        if ($cmd) {
            Add-AkSupportSection ("Get-AppPackageLog -ActivityID $activityId")
            $logText = Get-AppPackageLog -ActivityID $activityId -ErrorAction SilentlyContinue | Out-String
            if ([string]::IsNullOrWhiteSpace($logText)) {
                Add-AkSupportLine '(no AppX package log output)'
            } else {
                foreach ($line in ($logText -split "`r?`n")) { Add-AkSupportLine $line }
            }
        }
    } catch {
        Add-AkSupportLine ("Get-AppPackageLog failed: {0}" -f $_.Exception.Message)
    }
}

function Test-AppInstallerPresent {
    try {
        $pkg = Get-AppxPackage -Name Microsoft.DesktopAppInstaller -ErrorAction SilentlyContinue | Select-Object -First 1
        return ($null -ne $pkg)
    } catch {
        return $false
    }
}

function Get-AkRecoveryHint {
    param([string] $Reason)
    if ($Reason -match '0x80073CF3' -or $Reason -match 'WindowsAppRuntime') {
        $runtime = $script:WindowsAppRuntimeInstallerPath
        if ([string]::IsNullOrWhiteSpace($runtime)) { $runtime = '<runtime installer path was not recorded>' }
        return @(
            'Windows reported an App Installer dependency failure.',
            'Required dependency: Microsoft.WindowsAppRuntime.1.8 >= 8000.616.304.0',
            ("Runtime installer: {0}" -f $runtime),
            ("Manual retry: `"{0}`" --quiet" -f $runtime),
            'Then rerun Install-Agent-Kaizen.cmd from a fresh terminal.'
        )
    }
    if ($Reason -match 'direct download') {
        return @(
            'Git and/or Python could not be installed automatically.',
            'Install them manually, then rerun the installer:',
            '  Git for Windows : https://git-scm.com/download/win',
            '  Python 3.12+    : https://www.python.org/downloads/  (tick "Add python.exe to PATH")',
            'If they were just installed, open a fresh terminal and rerun.'
        )
    }
    if ($Reason -match 'Windows Package Manager|winget|WinGet') {
        return @(
            'If App Installer was installed or repaired, open a fresh terminal and rerun the installer.',
            'In Windows Sandbox, restart the sandbox session if a fresh terminal still cannot invoke winget.exe.',
            'Manual fallback: install Git for Windows and Python 3.12+, then rerun.'
        )
    }
    return @('Fix the issue above and rerun the installer. Reruns are safe and reuse completed work.')
}

function Write-AkFailureReport {
    param([Parameter(Mandatory)] $ErrorRecord)
    $reason = [string]$ErrorRecord.Exception.Message
    $failedStepName = 'Unknown'
    if ($script:CurrentStep -gt 0 -and $script:CurrentStep -le $script:Steps.Count) {
        $failedStepName = [string]$script:Steps[$script:CurrentStep - 1].Name
    }
    Add-AkSupportSection 'Failure'
    Add-AkSupportLine ("Failed step: {0}/{1} {2}" -f $script:CurrentStep, $script:Steps.Count, $failedStepName)
    Add-AkSupportLine ("Reason: {0}" -f $reason)
    if (-not [string]::IsNullOrWhiteSpace($script:LastCommandLogPath)) {
        Add-AkSupportLine ("Last command log: {0}" -f $script:LastCommandLogPath)
    }
    Write-Host ''
    Write-Host '================================================================' -ForegroundColor Red
    Write-Host (" Agent Kaizen setup FAILED at step {0}/{1}: {2}" -f $script:CurrentStep, $script:Steps.Count, $failedStepName) -ForegroundColor Red
    Write-Host '================================================================' -ForegroundColor Red
    Write-Host ("Reason: {0}" -f $reason) -ForegroundColor Red
    if (-not [string]::IsNullOrWhiteSpace($script:LastCommandLogPath)) {
        Write-Host ("Last command log: {0}" -f $script:LastCommandLogPath) -ForegroundColor Yellow
    }
    if (-not [string]::IsNullOrWhiteSpace($script:TranscriptLogPath)) {
        Write-Host ("Full transcript: {0}" -f $script:TranscriptLogPath) -ForegroundColor Yellow
    }
    if (-not [string]::IsNullOrWhiteSpace($script:SupportBundlePath)) {
        Write-Host ("Support bundle: {0}" -f $script:SupportBundlePath) -ForegroundColor Yellow
        Write-Host ("Copy support bundle: type `"{0}`" | clip" -f $script:SupportBundlePath) -ForegroundColor Yellow
    }
    if (-not [string]::IsNullOrWhiteSpace($script:LogRoot)) {
        Write-Host ("Logs folder: {0}" -f $script:LogRoot) -ForegroundColor Yellow
    }
    Write-Host ''
    Write-Host 'Recovery hint:' -ForegroundColor Yellow
    foreach ($line in (Get-AkRecoveryHint -Reason $reason)) {
        Write-Host ("  {0}" -f $line) -ForegroundColor Yellow
        Add-AkSupportLine ("Recovery: {0}" -f $line)
    }
}

function ConvertTo-JsonText {
    param([Parameter(Mandatory)] $Object)
    return (($Object | ConvertTo-Json -Depth 8) + [Environment]::NewLine)
}

function Resolve-AkFullPath {
    param([Parameter(Mandatory)][string] $Path)
    return [IO.Path]::GetFullPath($Path)
}

function Test-AkPathUnder {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $Parent
    )
    if ([string]::IsNullOrWhiteSpace($Parent)) { return $false }
    $childPath = (Resolve-AkFullPath $Path).TrimEnd('\')
    $parentPath = (Resolve-AkFullPath $Parent).TrimEnd('\')
    return ($childPath.Equals($parentPath, [StringComparison]::OrdinalIgnoreCase) -or $childPath.StartsWith(($parentPath + '\'), [StringComparison]::OrdinalIgnoreCase))
}

function Resolve-AkValidatedDevRoot {
    param([Parameter(Mandatory)][string] $Path)
    $candidate = ([string]$Path).Trim()
    $candidate = $candidate.Trim([char]'"')
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        throw 'DEVROOT cannot be blank. Choose D:\dev, C:\dev, or pass a custom folder path.'
    }
    $full = (Resolve-AkFullPath $candidate).TrimEnd('\')
    $root = [IO.Path]::GetPathRoot($full)
    if ([string]::IsNullOrWhiteSpace($root) -or -not (Test-Path -LiteralPath $root)) {
        throw "DEVROOT drive/root does not exist: $root"
    }
    if ($full.Equals($root.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw 'DEVROOT must be a folder path, not only a drive root.'
    }
    $protectedRoots = @($env:WINDIR, $env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    foreach ($protectedRoot in $protectedRoots) {
        if (Test-AkPathUnder -Path $full -Parent $protectedRoot) {
            throw "DEVROOT must not be inside a protected system folder: $protectedRoot"
        }
    }
    return $full
}

function Read-AkDevRootMenu {
    while ($true) {
        $dAvailable = Test-Path -LiteralPath 'D:\'
        Write-Host ''
        Write-Host 'Choose where Agent Kaizen should keep repos, tools, and setup logs:'
        if ($dAvailable) {
            Write-Host '  1) D:\dev  (recommended)'
        } else {
            Write-Host '  1) D:\dev  (D: drive not available)'
        }
        Write-Host '  2) C:\dev'
        Write-Host '  3) Custom path'
        $choice = Read-Host 'DEVROOT [1/2/3]'
        if ([string]::IsNullOrWhiteSpace($choice)) {
            if ($dAvailable) {
                $choice = '1'
            } else {
                Write-Warn 'D:\ is not available. Choose 2 for C:\dev or 3 for a custom path.'
                continue
            }
        }

        $candidate = $null
        switch ($choice.Trim()) {
            '1' {
                if (-not $dAvailable) {
                    Write-Warn 'D:\ is not available in this Windows session.'
                    continue
                }
                $candidate = 'D:\dev'
            }
            '2' { $candidate = 'C:\dev' }
            '3' {
                $answer = Read-Host 'Custom DEVROOT path'
                if ([string]::IsNullOrWhiteSpace($answer)) {
                    Write-Warn 'Custom DEVROOT cannot be blank.'
                    continue
                }
                $candidate = $answer
            }
            default {
                Write-Warn 'Choose 1, 2, or 3.'
                continue
            }
        }

        try {
            $validated = Resolve-AkValidatedDevRoot $candidate
        } catch {
            Write-Warn $_.Exception.Message
            continue
        }

        if ($AssumeYes) { return $validated }
        Write-Host ''
        Write-Host ("Selected DEVROOT: {0}" -f $validated)
        $confirm = Read-Host 'Use this location? [Y/n]'
        if ([string]::IsNullOrWhiteSpace($confirm) -or $confirm -match '^[Yy]([Ee][Ss])?$') {
            return $validated
        }
        if ($confirm -match '^[Nn]([Oo])?$') {
            continue
        }
        Write-Warn 'Please answer Y or N.'
    }
}

function Resolve-AkDevRoot {
    if ($DevRoot) {
        return (Resolve-AkValidatedDevRoot $DevRoot)
    }
    if ($env:DEVROOT) {
        return (Resolve-AkValidatedDevRoot $env:DEVROOT)
    }

    if ($ListSteps -or $PlanOnly -or $SelfTest) {
        return 'D:\dev'
    }

    if ($NoPrompt) {
        throw 'DEVROOT is required in non-interactive mode. Pass DEVROOT explicitly, for example: Install-Agent-Kaizen.cmd D:\dev -NoPrompt'
    }
    if ($AssumeYes) {
        if (Test-Path -LiteralPath 'D:\') {
            return (Resolve-AkValidatedDevRoot 'D:\dev')
        }
        throw 'DEVROOT is required with -AssumeYes when D:\ is unavailable. Pass DEVROOT explicitly, for example: Install-Agent-Kaizen.cmd X:\dev -AssumeYes'
    }

    return (Read-AkDevRootMenu)
}

function Initialize-AkContext {
    if ($RepoName -match '[\\/:*?"<>|]') {
        throw "RepoName contains characters that are not valid in a Windows folder name: $RepoName"
    }
    if (-not $Ref -and $env:AK_REF) {
        $script:Ref = $env:AK_REF
    }

    $script:DevRoot = Resolve-AkDevRoot
    $script:RepoPath = Join-Path $script:DevRoot $RepoName
    $script:VenvRoot = Join-Path $script:DevRoot 'Python\venvs\kaizen'
    $script:VenvPython = Join-Path $script:VenvRoot 'Scripts\python.exe'
    $script:PythonBaseDir = Join-Path $script:DevRoot 'Python\Python312'
    $script:RustRoot = Join-Path $script:DevRoot 'rust'
    $script:VSCodeRoot = Join-Path $script:DevRoot 'vscode'
    $script:DotNetRoot = Join-Path $script:DevRoot 'dotnet'
    $script:CMakeRoot = Join-Path $script:DevRoot 'cmake'
    $script:NodeRoot = Join-Path $script:DevRoot 'node'
    $script:NpmGlobalDir = Join-Path $script:NodeRoot 'npm-global'
    $script:WheelCache = Join-Path $script:DevRoot 'wheels'
    $script:WorkRoot = Join-Path $script:DevRoot 'agent-kaizen-setup'
    $script:LogRoot = Join-Path $script:WorkRoot 'logs'
    $script:StatePath = Join-Path $script:WorkRoot 'setup-state.json'
}

function Get-StepNumber {
    param([Parameter(Mandatory)][string] $Id)
    for ($i = 0; $i -lt $script:Steps.Count; $i++) {
        if ($script:Steps[$i].Id -eq $Id) { return ($i + 1) }
    }
    return ($script:CurrentStep + 1)
}

function Get-AkStepName {
    param([Parameter(Mandatory)][string] $Id)
    $s = $script:Steps | Where-Object { $_.Id -eq $Id } | Select-Object -First 1
    if ($s) { return $s.Name }
    return $Id
}

function Write-StepProgress {
    param([int] $Completed, [int] $StepNo, [string] $Status, [string] $Name)
    if ($NoProgressHeader) { return }
    $total = [Math]::Max(1, $script:Steps.Count)
    $pct = [Math]::Floor(($Completed * 100) / $total)
    $color = switch ($Status) {
        'OK'      { 'Green' }
        'SKIPPED' { 'DarkGray' }
        'working' { 'Cyan' }
        'FAILED'  { 'Red' }
        default   { 'Gray' }
    }
    Write-Host ("[{0,3}%] [{1}/{2}] {3}: {4}" -f $pct, $StepNo, $total, $Status, $Name) -ForegroundColor $color
}

function Set-AkStepSkipped {
    # Mark the current step as user-skipped (e.g. a deselected optional tool). Invoke-AkStep reads
    # $script:StepSkipped in its finally and reports SKIPPED instead of OK, so a skip reads distinctly.
    param([string] $Reason = '')
    $script:StepSkipped = $true
    $script:StepSkipReason = $Reason
    if ($Reason) { Write-Info "Skipped: $Reason" }
}

function Get-AkProgressBar {
    param([int] $Percent)
    $width = 34
    $pct = [Math]::Max(0, [Math]::Min(100, $Percent))
    $filled = [int][Math]::Floor(($pct / 100.0) * $width)
    if ($pct -ge 100) { return '[' + ('=' * $width) + ']' }
    if ($filled -lt 1) { $filled = 1 }
    $left = '=' * ($filled - 1)
    $right = '.' * ($width - $filled)
    return '[' + $left + '>' + $right + ']'
}

function Format-AkBannerLine {
    # Pad/truncate to the console width so each rewrite fully overwrites the prior line (no leftover
    # characters -> no flicker). Falls back to 80 columns if the host does not expose a buffer width.
    param([string] $Text)
    $w = 80
    try { $w = [Math]::Max(20, $Host.UI.RawUI.BufferSize.Width - 1) } catch { $w = 80 }
    if ($null -eq $Text) { $Text = '' }
    if ($Text.Length -gt $w) { return $Text.Substring(0, $w) }
    return $Text.PadRight($w)
}

function Write-AkBanner {
    # Redraw the pinned progress block in place: jump to the reserved region (re-pinned at the visible
    # window top so it stays sticky while output scrolls), rewrite every line in one pass, then restore
    # the cursor so normal output continues below. The whole body is guarded so an unsupported host
    # (redirected output, non-console) silently disables the banner instead of erroring.
    param(
        [int] $Completed = $script:BannerCompleted,
        [int] $StepNo = $script:BannerStepNo,
        [string] $Status = $script:BannerStatus,
        [string] $Name = $script:BannerName,
        [string] $Activity = $script:BannerActivity
    )
    if (-not $script:BannerEnabled) { return }
    $script:BannerCompleted = $Completed
    $script:BannerStepNo = $StepNo
    $script:BannerStatus = $Status
    $script:BannerName = $Name
    $script:BannerActivity = $Activity
    $total = [Math]::Max(1, $script:Steps.Count)
    $done = [Math]::Max(0, [Math]::Min($total, $Completed))
    $pct = [int][Math]::Floor(($done / [double]$total) * 100)
    $bar = Get-AkProgressBar -Percent $pct
    $stepText = if ($StepNo -le 0) { "Step 0/$total" } else { "Step $StepNo/$total" }
    $activityText = if ([string]::IsNullOrWhiteSpace($Activity)) { '  (waiting)' } else { "  $Activity" }
    $lines = @(
        ('== Agent Kaizen Setup ' + ('=' * 40)),
        ('  ' + $bar + ('  {0,3}%' -f $pct)),
        ('  ' + $stepText + '  ' + $Status + '  ::  ' + $Name),
        $activityText,
        ('  DEVROOT: ' + [string]$script:DevRoot),
        ('=' * 62)
    )
    try {
        $raw = $Host.UI.RawUI
        $old = $raw.CursorPosition
        $target = $old
        $target.X = 0
        $target.Y = [int][Math]::Max($script:BannerTop, $raw.WindowPosition.Y)
        $raw.CursorPosition = $target
        for ($i = 0; $i -lt $script:BannerHeight; $i++) {
            $text = if ($i -lt $lines.Count) { $lines[$i] } else { '' }
            $color = if ($i -eq 0 -or $i -eq ($script:BannerHeight - 1)) { 'DarkCyan' }
                     elseif ($i -eq 1) { 'Cyan' }
                     elseif ($i -eq 2) { 'Gray' }
                     else { 'DarkGray' }
            Write-Host (Format-AkBannerLine $text) -ForegroundColor $color
        }
        $raw.CursorPosition = $old
        $script:BannerLastPaintTicks = (Get-Date).Ticks
    } catch {
        $script:BannerEnabled = $false
    }
}

function Initialize-AkBanner {
    # Reserve the banner block at the current cursor position and enable in-place redraws. No-op under
    # -NoProgressHeader (automation) or on a host without RawUI cursor support.
    if ($NoProgressHeader) { return }
    try {
        $raw = $Host.UI.RawUI
        $null = $raw.CursorPosition
        $script:BannerTop = $raw.CursorPosition.Y
        for ($i = 0; $i -lt $script:BannerHeight; $i++) { Write-Host '' }
        $script:BannerEnabled = $true
        Write-AkBanner -Completed 0 -StepNo 0 -Status 'starting' -Name 'Initializing' -Activity ''
    } catch {
        $script:BannerEnabled = $false
    }
}

function Update-AkBannerActivity {
    # Live activity line (e.g. the current download). Throttled so the pinned block redraws only when
    # the integer percent changes or >=250ms since the last paint -> smooth, never flashing.
    param([string] $Text, [int] $Percent = -1)
    if (-not $script:BannerEnabled) { return }
    $elapsedMs = ((Get-Date).Ticks - $script:BannerLastPaintTicks) / 10000
    $pctChanged = ($Percent -ge 0 -and $Percent -ne $script:BannerLastActivityPct)
    if (-not $pctChanged -and $elapsedMs -lt 250) { return }
    $script:BannerLastActivityPct = $Percent
    Write-AkBanner -Activity $Text
}

function Show-AkPlan {
    Write-Host ''
    Write-Host 'Agent Kaizen installer (Windows)'
    Write-Host ("  DEVROOT : {0}" -f $script:DevRoot)
    Write-Host ("  Repo    : {0}" -f $script:RepoPath)
    Write-Host ("  Venv    : {0}" -f $script:VenvRoot)
    Write-Host ("  Logs    : {0}" -f $script:LogRoot)
    if ($RepoSource) { Write-Host ("  Source  : {0}" -f $RepoSource) }
    if ($Ref) { Write-Host ("  Ref     : {0}" -f $Ref) }
    Write-Host ''
    Write-Host 'Planned setup steps:'
    $total = [Math]::Max(1, $script:Steps.Count)
    for ($i = 0; $i -lt $script:Steps.Count; $i++) {
        $from = [Math]::Floor(($i * 100) / $total)
        $to = [Math]::Floor((($i + 1) * 100) / $total)
        Write-Host ("{0,2}. {1,-58} {2,3}% -> {3,3}%" -f ($i + 1), $script:Steps[$i].Name, $from, $to)
    }
}

function Write-AkPlanJson {
    param([Parameter(Mandatory)][string] $Path)
    $plan = [ordered]@{
        installer = 'Agent Kaizen installer (Windows)'
        devRoot = $script:DevRoot
        repoPath = $script:RepoPath
        venvPath = $script:VenvRoot
        logRoot = $script:LogRoot
        repoSource = $(if ($RepoSource) { $RepoSource } else { $RepoUrl })
        ref = $Ref
        planOnly = [bool]$PlanOnly
        selfTest = [bool]$SelfTest
        safety = [ordered]@{
            noNetwork = [bool]$NoNetwork
            noExternalActions = [bool]$NoExternalActions
            noUserEnvWrites = [bool]$NoUserEnvWrites
            noInput = [bool]$NoPrompt
        }
        steps = @()
    }
    for ($i = 0; $i -lt $script:Steps.Count; $i++) {
        $plan.steps += [ordered]@{
            index = $i + 1
            id = $script:Steps[$i].Id
            name = $script:Steps[$i].Name
        }
    }
    Write-Utf8NoBom -Path $Path -Content (ConvertTo-JsonText $plan)
    Write-Ok "Plan JSON written: $Path"
}

function Save-AkState {
    if ($PlanOnly -or $SelfTest) { return }
    $state = [ordered]@{
        installer = 'Agent Kaizen installer (Windows)'
        lastRun = (Get-Date).ToString('s')
        devRoot = $script:DevRoot
        repoPath = $script:RepoPath
        logRoot = $script:LogRoot
        steps = @($script:StepRecords)
    }
    Write-Utf8NoBom -Path $script:StatePath -Content (ConvertTo-JsonText $state)
}

function Assert-ExternalAllowed {
    param([Parameter(Mandatory)][string] $Action)
    if ($PlanOnly -or $SelfTest -or $NoExternalActions) {
        throw "External action blocked by installer safety mode: $Action"
    }
}

function Assert-NetworkAllowed {
    param([Parameter(Mandatory)][string] $Action)
    if ($PlanOnly -or $SelfTest -or $NoNetwork -or $NoExternalActions) {
        throw "Network action blocked by installer safety mode: $Action"
    }
}

function Assert-UserEnvWritesAllowed {
    param([Parameter(Mandatory)][string] $Action)
    if ($PlanOnly -or $SelfTest -or $NoUserEnvWrites) {
        throw "User environment write blocked by installer safety mode: $Action"
    }
}

function Invoke-AkStep {
    param(
        [Parameter(Mandatory)][string] $Id,
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)][scriptblock] $Body
    )
    $stepNo = Get-StepNumber -Id $Id
    $script:CurrentStep = $stepNo
    $before = $stepNo - 1
    $start = Get-Date
    $status = 'OK'
    $reason = ''
    $script:StepSkipped = $false
    $script:StepSkipReason = ''

    Write-StepProgress -Completed $before -StepNo $stepNo -Status 'working' -Name $Name
    Write-AkBanner -Completed $before -StepNo $stepNo -Status 'working' -Name $Name -Activity ''
    Write-Host ''
    Write-Host ("=== [{0}/{1}] {2} ===" -f $stepNo, $script:Steps.Count, $Name) -ForegroundColor Cyan

    try {
        if ($PlanOnly) {
            Write-Info 'Plan-only: step not executed.'
            $status = 'PLAN'
        } elseif ($SelfTest) {
            Write-Info 'Self-test: validating step shape only; no external actions.'
            $status = 'SELFTEST'
        } else {
            & $Body
        }
    } catch {
        $status = 'FAILED'
        $reason = $_.Exception.Message
        throw
    } finally {
        if ($status -eq 'OK' -and $script:StepSkipped) {
            $status = 'SKIPPED'
            if ($script:StepSkipReason) { $reason = $script:StepSkipReason }
        }
        $elapsed = [Math]::Round(((Get-Date) - $start).TotalSeconds, 2)
        [void]$script:StepRecords.Add([ordered]@{
            id = $Id
            name = $Name
            status = $status
            elapsedSeconds = $elapsed
            reason = $reason
        })
        Save-AkState
        if ($status -eq 'FAILED') {
            Write-StepProgress -Completed $before -StepNo $stepNo -Status $status -Name $Name
            Write-AkBanner -Completed $before -StepNo $stepNo -Status $status -Name $Name -Activity ''
        } else {
            Write-StepProgress -Completed $stepNo -StepNo $stepNo -Status $status -Name $Name
            Write-AkBanner -Completed $stepNo -StepNo $stepNo -Status $status -Name $Name -Activity ''
        }
    }
}

function Quote-NativeArg {
    param([object] $Value)
    if ($null -eq $Value) { return '""' }
    $s = [string]$Value
    if ($s.Length -eq 0) { return '""' }
    if ($s -notmatch '[\s"]') { return $s }
    $escaped = [regex]::Replace($s, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Join-NativeCommandLine {
    param([Parameter(Mandatory)][string] $FilePath, [string[]] $ArgumentList = @())
    $parts = New-Object System.Collections.Generic.List[string]
    $parts.Add((Quote-NativeArg $FilePath))
    foreach ($arg in $ArgumentList) { $parts.Add((Quote-NativeArg $arg)) }
    return ($parts -join ' ')
}

function New-CommandLogPath {
    param([Parameter(Mandatory)][string] $FilePath)
    if (-not (Test-Path -LiteralPath $script:LogRoot)) {
        New-Item -ItemType Directory -Path $script:LogRoot -Force | Out-Null
    }
    $script:CommandCounter++
    $leaf = [IO.Path]::GetFileNameWithoutExtension($FilePath)
    if (-not $leaf) { $leaf = 'command' }
    $leaf = ($leaf -replace '[^A-Za-z0-9_.-]', '_')
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    return (Join-Path $script:LogRoot ("command-{0}-{1:D3}-{2}.log" -f $stamp, $script:CommandCounter, $leaf))
}

function Get-ShortNativeLine {
    param([string] $Line)
    if (-not $Line) { return '' }
    $clean = $Line -replace '[\r\n]+', ' '
    if ($clean.Length -gt 110) { return ($clean.Substring(0, 107) + '...') }
    return $clean
}

function Invoke-Native {
    param(
        [Parameter(Mandatory)][string] $FilePath,
        [string[]] $ArgumentList = @(),
        [string] $WorkingDirectory,
        [string] $Note,
        [switch] $Network
    )
    if ($Network) { Assert-NetworkAllowed (Join-NativeCommandLine $FilePath $ArgumentList) }
    else { Assert-ExternalAllowed (Join-NativeCommandLine $FilePath $ArgumentList) }

    $log = New-CommandLogPath -FilePath $FilePath
    $commandLine = Join-NativeCommandLine $FilePath $ArgumentList
    $script:LastCommandLogPath = $log
    $script:LastCommandLine = $commandLine
    $script:LastActivityNote = $Note

    $stdout = Join-Path $script:LogRoot ("native-stdout-" + [guid]::NewGuid().ToString('N') + ".tmp")
    $stderr = Join-Path $script:LogRoot ("native-stderr-" + [guid]::NewGuid().ToString('N') + ".tmp")
    $exitCode = 9999
    $start = Get-Date
    $started = $false
    $proc = $null
    $finalWritten = $false
    $header = "COMMAND: {0}`r`nSTARTED: {1}`r`nNOTE: {2}`r`n" -f $commandLine, $start.ToString('s'), $Note
    if ($WorkingDirectory) { $header += ("WORKDIR: {0}`r`n" -f $WorkingDirectory) }
    $header += "RUNNER: Start-Process with redirected stdout/stderr temp files.`r`nSENTINEL EXIT CODE: 9999`r`n"
    Write-Utf8NoBom -Path $log -Content $header

    try {
        $argLine = (($ArgumentList | ForEach-Object { Quote-NativeArg $_ }) -join ' ')
        $startArgs = @{
            FilePath = $FilePath
            RedirectStandardOutput = $stdout
            RedirectStandardError = $stderr
            PassThru = $true
            NoNewWindow = $true
        }
        if (-not [string]::IsNullOrWhiteSpace($argLine)) { $startArgs.ArgumentList = $argLine }
        if ($WorkingDirectory) { $startArgs.WorkingDirectory = $WorkingDirectory }

        $proc = Start-Process @startArgs
        $started = $true
        Add-Content -LiteralPath $log -Value ("PID: {0}" -f $proc.Id) -Encoding UTF8
        $frames = @('|', '/', '-', '\')
        $lastLine = ''
        while ($null -ne $proc -and -not $proc.HasExited) {
            Start-Sleep -Milliseconds 900
            try { $proc.Refresh() } catch {}
            try {
                if (Test-Path -LiteralPath $stdout) {
                    $tail = @(Get-Content -LiteralPath $stdout -Tail 1 -ErrorAction SilentlyContinue)
                    if ($tail.Count -gt 0) { $lastLine = [string]$tail[$tail.Count - 1] }
                }
                if ([string]::IsNullOrWhiteSpace($lastLine) -and (Test-Path -LiteralPath $stderr)) {
                    $tailErr = @(Get-Content -LiteralPath $stderr -Tail 1 -ErrorAction SilentlyContinue)
                    if ($tailErr.Count -gt 0) { $lastLine = [string]$tailErr[$tailErr.Count - 1] }
                }
            } catch {}
            if (-not $NoProgressHeader) {
                $elapsed = (Get-Date) - $start
                $frame = $frames[[int]($elapsed.TotalSeconds) % $frames.Count]
                $tail = Get-ShortNativeLine $lastLine
                Write-Host -NoNewline ("`r[{0}] step {1}/{2} elapsed {3:hh\:mm\:ss} {4,-78}" -f $frame, $script:CurrentStep, $script:Steps.Count, $elapsed, $tail)
            }
        }
        if ($null -ne $proc) {
            $proc.WaitForExit()
            $exitCode = [int]$proc.ExitCode
        }
        if (-not $NoProgressHeader) { Write-Host '' }

        if (Test-Path -LiteralPath $stdout) {
            $stdoutLines = @(Get-Content -LiteralPath $stdout -ErrorAction SilentlyContinue)
            if ($stdoutLines.Count -gt 0) {
                Add-Content -LiteralPath $log -Value '' -Encoding UTF8
                Add-Content -LiteralPath $log -Value 'STDOUT:' -Encoding UTF8
                foreach ($line in $stdoutLines) { Add-Content -LiteralPath $log -Value $line -Encoding UTF8 }
            }
        }
        if (Test-Path -LiteralPath $stderr) {
            $stderrLines = @(Get-Content -LiteralPath $stderr -ErrorAction SilentlyContinue)
            if ($stderrLines.Count -gt 0) {
                Add-Content -LiteralPath $log -Value '' -Encoding UTF8
                Add-Content -LiteralPath $log -Value 'STDERR:' -Encoding UTF8
                foreach ($line in $stderrLines) { Add-Content -LiteralPath $log -Value $line -Encoding UTF8 }
            }
        }

        $elapsedFinal = (Get-Date) - $start
        Add-Content -LiteralPath $log -Value '' -Encoding UTF8
        Add-Content -LiteralPath $log -Value ("FINISHED: {0}" -f (Get-Date).ToString('s')) -Encoding UTF8
        Add-Content -LiteralPath $log -Value ("EXIT CODE: {0}" -f $exitCode) -Encoding UTF8
        Add-Content -LiteralPath $log -Value ("ELAPSED: {0:hh\:mm\:ss}" -f $elapsedFinal) -Encoding UTF8
        $finalWritten = $true

        if ($exitCode -ne 0) {
            Write-Host "Command failed with exit code $exitCode. Log: $log" -ForegroundColor Red
            try {
                Get-Content -LiteralPath $log -Tail 12 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
            } catch {}
            throw ('Command failed with exit code {0}: {1}' -f $exitCode, $commandLine)
        }
        Write-Ok "Command completed. Log: $log"
        return $log
    } catch {
        $elapsedFinal = (Get-Date) - $start
        if ($finalWritten) {
            try { Add-Content -LiteralPath $log -Value ("ERROR AFTER FINAL STATUS: {0}" -f $_.Exception.Message) -Encoding UTF8 } catch {}
            throw
        }
        try {
            if (Test-Path -LiteralPath $stdout) {
                $stdoutLines = @(Get-Content -LiteralPath $stdout -ErrorAction SilentlyContinue)
                if ($stdoutLines.Count -gt 0) {
                    Add-Content -LiteralPath $log -Value '' -Encoding UTF8
                    Add-Content -LiteralPath $log -Value 'STDOUT:' -Encoding UTF8
                    foreach ($line in $stdoutLines) { Add-Content -LiteralPath $log -Value $line -Encoding UTF8 }
                }
            }
            if (Test-Path -LiteralPath $stderr) {
                $stderrLines = @(Get-Content -LiteralPath $stderr -ErrorAction SilentlyContinue)
                if ($stderrLines.Count -gt 0) {
                    Add-Content -LiteralPath $log -Value '' -Encoding UTF8
                    Add-Content -LiteralPath $log -Value 'STDERR:' -Encoding UTF8
                    foreach ($line in $stderrLines) { Add-Content -LiteralPath $log -Value $line -Encoding UTF8 }
                }
            }
            Add-Content -LiteralPath $log -Value '' -Encoding UTF8
            Add-Content -LiteralPath $log -Value ("ERROR BEFORE/AROUND EXIT CODE CAPTURE: {0}" -f $_.Exception.Message) -Encoding UTF8
            Add-Content -LiteralPath $log -Value ("FINISHED: {0}" -f (Get-Date).ToString('s')) -Encoding UTF8
            Add-Content -LiteralPath $log -Value ("EXIT CODE: {0}" -f $exitCode) -Encoding UTF8
            Add-Content -LiteralPath $log -Value ("ELAPSED: {0:hh\:mm\:ss}" -f $elapsedFinal) -Encoding UTF8
        } catch {}
        throw
    } finally {
        if ($null -ne $proc) { try { $proc.Dispose() } catch {} }
        try { Remove-Item -LiteralPath $stdout,$stderr -Force -ErrorAction SilentlyContinue } catch {}
    }
}

function Invoke-AkInstaller {
    # Job-based installer runner, mirroring the SC installer's Invoke-LoggedCommandJob. Invoke-Native
    # launches with Start-Process -NoNewWindow and redirected std handles, which makes WiX-burn
    # bootstrappers (python.org, VS Build Tools) hand off to a child and return in ~1s with exit 0
    # while nothing installs. Running the .exe with the call operator inside a background job (SC's
    # proven path) keeps it in-process so it returns its REAL exit code. Returns the exit code and
    # never throws on non-zero: the caller validates the result on disk (3010 = reboot-pending and
    # other non-zero codes can still leave a working install).
    param(
        [Parameter(Mandatory)][string] $FilePath,
        [string[]] $ArgumentList = @(),
        [string] $Note,
        [switch] $Network
    )
    if ($Network) { Assert-NetworkAllowed (Join-NativeCommandLine $FilePath $ArgumentList) }
    else { Assert-ExternalAllowed (Join-NativeCommandLine $FilePath $ArgumentList) }

    $log = New-CommandLogPath -FilePath $FilePath
    $commandLine = Join-NativeCommandLine $FilePath $ArgumentList
    $script:LastCommandLogPath = $log
    $script:LastCommandLine = $commandLine
    $script:LastActivityNote = $Note

    $start = Get-Date
    $header = "COMMAND: {0}`r`nSTARTED: {1}`r`nNOTE: {2}`r`nRUNNER: Start-Job call operator (in-process, inherited handles).`r`n`r`n" -f $commandLine, $start.ToString('s'), $Note
    Write-Utf8NoBom -Path $log -Content $header

    $exitFile = "$log.exit"
    Remove-Item -LiteralPath $exitFile -Force -ErrorAction SilentlyContinue

    $job = Start-Job -ScriptBlock {
        param([string] $JobExe, [string[]] $JobArgs, [string] $JobLog, [string] $JobExitFile)
        $ErrorActionPreference = 'Continue'
        try {
            & $JobExe @JobArgs 2>&1 | ForEach-Object {
                try { Add-Content -LiteralPath $JobLog -Value ($_.ToString()) -Encoding UTF8 } catch {}
            }
            $ec = $LASTEXITCODE
            if ($null -eq $ec) { $ec = 0 }
        } catch {
            try { Add-Content -LiteralPath $JobLog -Value ("ERROR: " + $_.Exception.Message) -Encoding UTF8 } catch {}
            $ec = 1
        }
        try { Set-Content -LiteralPath $JobExitFile -Value ([string]$ec) -Encoding ASCII } catch {}
    } -ArgumentList $FilePath, ([string[]]$ArgumentList), $log, $exitFile

    $frames = @('|', '/', '-', '\')
    while ((Get-Job -Id $job.Id).State -eq 'Running') {
        Start-Sleep -Milliseconds 900
        if (-not $NoProgressHeader) {
            $elapsed = (Get-Date) - $start
            $frame = $frames[[int]($elapsed.TotalSeconds) % $frames.Count]
            Write-Host -NoNewline ("`r[{0}] step {1}/{2} elapsed {3:hh\:mm\:ss} {4,-64}" -f $frame, $script:CurrentStep, $script:Steps.Count, $elapsed, (Get-ShortNativeLine $Note))
        }
    }
    Wait-Job -Job $job | Out-Null
    Receive-Job -Job $job -ErrorAction SilentlyContinue | Out-Null
    Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
    if (-not $NoProgressHeader) { Write-Host '' }

    $exitCode = 9999
    if (Test-Path -LiteralPath $exitFile) {
        try { $exitCode = [int]((Get-Content -LiteralPath $exitFile -ErrorAction Stop | Select-Object -First 1).Trim()) } catch { $exitCode = 9999 }
        Remove-Item -LiteralPath $exitFile -Force -ErrorAction SilentlyContinue
    }

    $elapsedFinal = (Get-Date) - $start
    Add-Content -LiteralPath $log -Value '' -Encoding UTF8
    Add-Content -LiteralPath $log -Value ("FINISHED: {0}" -f (Get-Date).ToString('s')) -Encoding UTF8
    Add-Content -LiteralPath $log -Value ("EXIT CODE: {0}" -f $exitCode) -Encoding UTF8
    Add-Content -LiteralPath $log -Value ("ELAPSED: {0:hh\:mm\:ss}" -f $elapsedFinal) -Encoding UTF8

    if ($exitCode -eq 0) {
        Write-Ok "Command completed. Log: $log"
    } else {
        Write-Warn "Installer exit code $exitCode (may still have succeeded; validating on disk). Log: $log"
    }
    return $exitCode
}

function Test-AkDownloadedFile {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][ValidateSet('zip', 'exe')][string] $Kind,
        [Parameter(Mandatory)][string] $Uri,
        [int64] $MinimumBytes = 1MB
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Download did not produce a file: $Path (from $Uri)"
    }
    $item = Get-Item -LiteralPath $Path
    $magic = New-Object byte[] 2
    $read = 0
    $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try { $read = $fs.Read($magic, 0, 2) } finally { $fs.Dispose() }
    # appx/msix/msixbundle/zip start with 'PK' (0x50 0x4B); .exe starts with 'MZ' (0x4D 0x5A).
    $expected = if ($Kind -eq 'zip') { @(0x50, 0x4B) } else { @(0x4D, 0x5A) }
    $valid = ($read -eq 2 -and $magic[0] -eq $expected[0] -and $magic[1] -eq $expected[1] -and $item.Length -ge $MinimumBytes)
    if (-not $valid) {
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        throw ("Downloaded file is not a valid {0} ({1:n1} MB, expected at least {2:n1} MB): {3}`r`nSource URL: {4}`r`nThe server likely returned an HTML or redirect page instead of the package. The invalid file was deleted; rerun to retry." -f $Kind, ($item.Length / 1MB), ($MinimumBytes / 1MB), $Path, $Uri)
    }
}

function Invoke-AkDownload {
    param(
        [Parameter(Mandatory)][string] $Uri,
        [Parameter(Mandatory)][string] $OutFile,
        [string] $Label = 'download',
        [ValidateSet('zip', 'exe')][string] $Verify,
        [int64] $MinimumBytes = 1MB,
        [switch] $Force
    )
    # Pre-flight (Verify pillar): reuse an already-downloaded, still-valid file instead of fetching
    # it again on a warm re-run. Test-AkDownloadedFile deletes a corrupt cache and throws, so an
    # invalid cache falls through to a fresh download.
    if (-not $Force -and (Test-Path -LiteralPath $OutFile)) {
        $cachedOk = $true
        if ($Verify) {
            try { Test-AkDownloadedFile -Path $OutFile -Kind $Verify -Uri $Uri -MinimumBytes $MinimumBytes } catch { $cachedOk = $false }
        }
        if ($cachedOk) { Write-Info ("{0} already downloaded: {1}" -f $Label, $OutFile); return }
    }
    Assert-NetworkAllowed $Uri
    $parent = Split-Path -Parent $OutFile
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    $request = [Net.HttpWebRequest]::Create($Uri)
    $request.UserAgent = 'AgentKaizenInstaller/1.0'
    $response = $request.GetResponse()
    try {
        $total = [int64]$response.ContentLength
        $inputStream = $response.GetResponseStream()
        $outputStream = [IO.File]::Open($OutFile, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $buffer = New-Object byte[] (1024 * 512)
            $downloaded = [int64]0
            $start = Get-Date
            while ($true) {
                $read = $inputStream.Read($buffer, 0, $buffer.Length)
                if ($read -le 0) { break }
                $outputStream.Write($buffer, 0, $read)
                $downloaded += $read
                if (-not $NoProgressHeader) {
                    $elapsed = [Math]::Max(0.25, ((Get-Date) - $start).TotalSeconds)
                    $rate = $downloaded / $elapsed
                    if ($total -gt 0) {
                        $pct = [Math]::Min(100, [Math]::Floor(($downloaded * 100) / $total))
                        $remaining = [Math]::Max(0, $total - $downloaded)
                        $eta = if ($rate -gt 0) { [TimeSpan]::FromSeconds($remaining / $rate) } else { [TimeSpan]::Zero }
                        $line = ("{0}: {1:n1}/{2:n1} MB ({3}%) {4:n1} MB/s ETA {5:hh\:mm\:ss}" -f $Label, ($downloaded / 1MB), ($total / 1MB), $pct, ($rate / 1MB), $eta)
                        if ($script:BannerEnabled) { Update-AkBannerActivity -Text ("down: " + $line) -Percent $pct }
                        else { Write-Host -NoNewline ("`r[{0}] {1}   " -f $script:CurrentStep, $line) }
                    } else {
                        $line = ("{0}: {1:n1} MB downloaded, total unknown, {2:n1} MB/s" -f $Label, ($downloaded / 1MB), ($rate / 1MB))
                        if ($script:BannerEnabled) { Update-AkBannerActivity -Text ("down: " + $line) }
                        else { Write-Host -NoNewline ("`r[{0}] {1}   " -f $script:CurrentStep, $line) }
                    }
                }
            }
            if (-not $NoProgressHeader -and -not $script:BannerEnabled) { Write-Host '' }
            Write-Ok "$Label downloaded: $OutFile"
        } finally {
            $outputStream.Dispose()
            $inputStream.Dispose()
        }
    } finally {
        $response.Dispose()
    }
    if ($Verify) { Test-AkDownloadedFile -Path $OutFile -Kind $Verify -Uri $Uri -MinimumBytes $MinimumBytes }
}

function Update-ProcessPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @($machine, $user) | Where-Object { $_ }
    if ($parts) { $env:Path = ($parts -join ';') }
}

function Test-IsAdmin {
    try {
        $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Get-AkCpuArch {
    if ($env:PROCESSOR_ARCHITECTURE -match 'ARM64') { return 'arm64' }
    if ($env:PROCESSOR_ARCHITECTURE -match '86') { return 'x86' }
    return 'x64'
}

function Find-Winget {
    Update-ProcessPath
    $candidates = New-Object System.Collections.Generic.List[string]
    $cmd = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($cmd) { $candidates.Add($cmd.Source) }
    if ($env:LOCALAPPDATA) {
        $candidates.Add((Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\winget.exe'))
    }
    foreach ($pkg in @('Microsoft.DesktopAppInstaller', 'Microsoft.Winget.Source')) {
        try {
            Get-AppxPackage -Name $pkg -ErrorAction SilentlyContinue |
                Where-Object { $_.InstallLocation } |
                ForEach-Object { $candidates.Add((Join-Path $_.InstallLocation 'winget.exe')) }
        } catch {}
    }
    if ($env:ProgramFiles) {
        $windowsApps = Join-Path $env:ProgramFiles 'WindowsApps'
        if (Test-Path -LiteralPath $windowsApps) {
            try {
                Get-ChildItem -LiteralPath $windowsApps -Filter winget.exe -Recurse -ErrorAction SilentlyContinue |
                    Sort-Object FullName -Descending |
                    ForEach-Object { $candidates.Add($_.FullName) }
            } catch {}
        }
    }
    foreach ($path in $candidates) {
        if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    return $null
}

function Test-WingetNow {
    param([string] $Path)
    if (-not $Path) { $Path = Find-Winget }
    if (-not $Path) { return $false }
    try {
        $out = & $Path --version 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return $true }
    } catch {}
    return $false
}

function Wait-ForWinget {
    # After registering the App Installer package the winget.exe execution alias / PATH entry can
    # lag by a moment; poll briefly before concluding the register step did not take.
    param([int] $Attempts = 6)
    for ($i = 0; $i -lt $Attempts; $i++) {
        Update-ProcessPath
        if (Test-WingetNow) { return $true }
        Start-Sleep -Milliseconds 800
    }
    return $false
}

function Get-WindowsAppRuntime18Uri {
    # Windows App SDK 1.8 runtime redist (arch-aware aka.ms redirect).
    switch (Get-AkCpuArch) {
        'arm64' { return 'https://aka.ms/windowsappsdk/1.8/latest/windowsappruntimeinstall-arm64.exe' }
        'x86'   { return 'https://aka.ms/windowsappsdk/1.8/latest/windowsappruntimeinstall-x86.exe' }
        default { return 'https://aka.ms/windowsappsdk/1.8/latest/windowsappruntimeinstall-x64.exe' }
    }
}

function Ensure-WindowsAppRuntime18 {
    # Best-effort: the current Microsoft.DesktopAppInstaller bundle depends on the
    # Microsoft.WindowsAppRuntime.1.8 framework. A fresh Windows Sandbox ships App Installer WITHOUT
    # that framework, so both RegisterByFamilyName (0x80073CF9) and the direct VCLibs+UI.Xaml+bundle
    # recipe (0x80073CF3) fail until it is present. Installing the WindowsAppRuntime 1.8 redist first
    # is what let winget install in the sandbox (proven by the earlier build's mirrored logs); soft-
    # fail here since registration/bundle may still succeed if the framework is already present.
    # Pre-flight (Verify pillar): skip the ~100 MB download + install entirely if the framework is
    # already present (warm re-run / already staged).
    try {
        if (Get-AppxPackage -Name 'Microsoft.WindowsAppRuntime.1.8' -ErrorAction SilentlyContinue | Select-Object -First 1) {
            Write-Info 'Windows App Runtime 1.8 already present; skipping.'
            return
        }
    } catch {}
    $arch = Get-AkCpuArch
    $uri = Get-WindowsAppRuntime18Uri
    $downloadRoot = Join-Path $script:WorkRoot 'downloads\winget'
    New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
    $installer = Join-Path $downloadRoot ("WindowsAppRuntimeInstall-1.8.$arch.exe")
    try {
        Write-Info 'Installing the Windows App Runtime 1.8 framework the App Installer bundle depends on.'
        Invoke-AkDownload -Uri $uri -OutFile $installer -Label 'Windows App Runtime 1.8' -Verify exe -MinimumBytes 1MB
        [void](Invoke-AkInstaller -FilePath $installer -ArgumentList @('--quiet') -Network -Note 'Installing Windows App Runtime 1.8 (App Installer dependency).')
        $script:WindowsAppRuntimeInstallerPath = $installer
    } catch {
        Write-Warn "Windows App Runtime 1.8 install did not complete (continuing; registration/bundle may still work): $($_.Exception.Message)"
    }
}

function Register-AppInstaller {
    Assert-ExternalAllowed 'register App Installer package alias'
    Write-Info 'Enabling winget by registering the App Installer package (works in a warm sandbox):'
    Write-Info '  Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe'
    Write-AkPackageInventory -Label 'before App Installer registration'
    try {
        Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe -ErrorAction Stop | Out-Null
        Write-AkPackageInventory -Label 'after App Installer family registration'
        if (Wait-ForWinget) { return $true }
    } catch {
        Add-AkAppxFailureEvidence -ErrorRecord $_
        Write-Warn "App Installer registration by family name did not complete: $($_.Exception.Message)"
        Write-AkPackageInventory -Label 'after failed App Installer family registration'
    }

    try {
        $pkg = Get-AppxPackage -Name Microsoft.DesktopAppInstaller -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($pkg -and $pkg.InstallLocation) {
            $manifest = Join-Path $pkg.InstallLocation 'AppxManifest.xml'
            if (Test-Path -LiteralPath $manifest) {
                Add-AppxPackage -DisableDevelopmentMode -Register $manifest -ErrorAction Stop | Out-Null
                Write-AkPackageInventory -Label 'after App Installer manifest registration'
                if (Wait-ForWinget) { return $true }
            }
        }
    } catch {
        Add-AkAppxFailureEvidence -ErrorRecord $_
        Write-Warn "App Installer manifest registration did not complete: $($_.Exception.Message)"
        Write-AkPackageInventory -Label 'after failed App Installer manifest registration'
    }
    return $false
}

function Invoke-WinGetRepair {
    Assert-NetworkAllowed 'Repair-WinGetPackageManager'
    Assert-ExternalAllowed 'Repair-WinGetPackageManager'
    Write-Info 'Trying Microsoft.WinGet.Client Repair-WinGetPackageManager.'
    try {
        Install-PackageProvider -Name NuGet -Force -Scope CurrentUser -ErrorAction Stop | Out-Null
        try { Set-PSRepository -Name PSGallery -InstallationPolicy Trusted -ErrorAction SilentlyContinue } catch {}
        Install-Module -Name Microsoft.WinGet.Client -Force -Repository PSGallery -Scope CurrentUser -AllowClobber -ErrorAction Stop | Out-Null
        Import-Module Microsoft.WinGet.Client -ErrorAction Stop
        $repairCommand = Get-Command Repair-WinGetPackageManager -CommandType Cmdlet -ErrorAction Stop
        try {
            & $repairCommand -ErrorAction Stop
        } catch {
            if (Test-IsAdmin) {
                Write-Warn "Current-user repair failed; trying all-users repair: $($_.Exception.Message)"
                & $repairCommand -AllUsers -ErrorAction Stop
            } else {
                throw
            }
        }
        Update-ProcessPath
        if (Test-WingetNow) { return $true }
    } catch {
        Write-Warn "Repair-WinGetPackageManager did not complete: $($_.Exception.Message)"
    }
    return $false
}

function Get-LatestWinGetBundleUri {
    # SC-style: the App Installer bundle from the latest winget-cli release, aka.ms/getwinget fallback.
    Assert-NetworkAllowed 'WinGet latest release lookup'
    try {
        $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/microsoft/winget-cli/releases/latest' -Headers @{ 'User-Agent' = 'AgentKaizenInstaller' } -ErrorAction Stop
        $bundle = $release.assets | Where-Object { $_.name -eq 'Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle' } | Select-Object -First 1
        if ($bundle -and $bundle.browser_download_url) { return $bundle.browser_download_url }
    } catch {
        Write-Warn "Could not query latest winget release: $($_.Exception.Message)"
    }
    return 'https://aka.ms/getwinget'
}

function Install-WinGetDirect {
    # Last-resort winget bootstrap, matching the SC installer's Try-DirectWinGetPackageInstall:
    # download the Desktop VCLibs + UI.Xaml deps and the App Installer bundle, install the deps
    # best-effort (soft-fail), then the bundle. This runs only when registration + Repair both fail;
    # in the owner's sandbox the App Installer is already present, so registration alone works.
    Assert-NetworkAllowed 'direct App Installer package download'
    Assert-ExternalAllowed 'direct App Installer package install'
    $downloadRoot = Join-Path $script:WorkRoot 'downloads\winget'
    New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
    Write-AkPackageInventory -Label 'before direct WinGet/App Installer bootstrap'

    try {
        $arch = Get-AkCpuArch
        $vclibsUri = "https://aka.ms/Microsoft.VCLibs.$arch.14.00.Desktop.appx"
        $xamlUri = "https://github.com/microsoft/microsoft-ui-xaml/releases/download/v2.8.6/Microsoft.UI.Xaml.2.8.$arch.appx"
        $vclibsPath = Join-Path $downloadRoot "Microsoft.VCLibs.$arch.14.00.Desktop.appx"
        $xamlPath = Join-Path $downloadRoot "Microsoft.UI.Xaml.2.8.$arch.appx"
        $bundlePath = Join-Path $downloadRoot 'Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle'
        $bundleUri = Get-LatestWinGetBundleUri

        Invoke-AkDownload -Uri $vclibsUri -OutFile $vclibsPath -Label 'VCLibs Desktop' -Verify zip -MinimumBytes 1MB
        Invoke-AkDownload -Uri $xamlUri -OutFile $xamlPath -Label 'UI.Xaml' -Verify zip -MinimumBytes 1MB
        Invoke-AkDownload -Uri $bundleUri -OutFile $bundlePath -Label 'App Installer' -Verify zip -MinimumBytes 20MB

        # Pre-flight (Verify pillar): only install an appx dependency the machine does not already have.
        $deps = @(
            @{ Path = $vclibsPath; Name = 'Microsoft.VCLibs.140.00.UWPDesktop' },
            @{ Path = $xamlPath;   Name = 'Microsoft.UI.Xaml.2.8' }
        )
        foreach ($dep in $deps) {
            try {
                if (Get-AppxPackage -Name $dep.Name -ErrorAction SilentlyContinue | Select-Object -First 1) {
                    Write-Info ("Dependency already present: {0}" -f $dep.Name); continue
                }
                Write-Info "Installing dependency: $(Split-Path -Leaf $dep.Path)"
                Add-AppxPackage -Path $dep.Path -ForceApplicationShutdown -ErrorAction Stop | Out-Null
            } catch {
                Write-Warn "Dependency install skipped or already satisfied: $($_.Exception.Message)"
            }
        }
        if (Get-AppxPackage -Name 'Microsoft.DesktopAppInstaller' -ErrorAction SilentlyContinue | Select-Object -First 1) {
            Write-Info 'App Installer bundle already present; registering only.'
        } else {
            Write-Info 'Installing Microsoft Desktop App Installer / WinGet bundle...'
            Add-AppxPackage -Path $bundlePath -ForceApplicationShutdown -ErrorAction Stop | Out-Null
        }
        try { Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe -ErrorAction SilentlyContinue } catch {}
        Update-ProcessPath
        return (Test-WingetNow)
    } finally {
        Write-AkPackageInventory -Label 'after direct WinGet/App Installer bootstrap'
    }
}

function Write-WingetUnavailable {
    # Non-fatal: winget is optional. Git and Python have direct-download fallbacks, so record the
    # unavailable state and let the run continue instead of aborting the whole installer.
    $script:WingetUnavailable = $true
    $script:Winget = $null
    $appInstaller = $null
    try { $appInstaller = Get-AppxPackage -Name Microsoft.DesktopAppInstaller -ErrorAction SilentlyContinue | Select-Object -First 1 } catch {}
    Write-AkPackageInventory -Label 'winget unavailable final state'
    if ($appInstaller -and -not (Test-WingetNow)) {
        Write-Warn 'App Installer is installed, but winget.exe is not callable in this terminal yet.'
        Write-Info 'winget may work from a fresh terminal (or after a Windows Sandbox session restart).'
    }
    Write-Warn 'Continuing without winget. Git and Python will be installed by direct download if missing.'
}

function Ensure-Winget {
    $path = Find-Winget
    if ($path -and (Test-WingetNow $path)) {
        $script:Winget = $path
        Write-Ok "winget available: $path"
        return
    }
    $script:Winget = $null
    if ($script:WingetBootstrapAttempted) { return }
    $script:WingetBootstrapAttempted = $true

    if ($NoNetwork -or $NoExternalActions) {
        Write-Warn 'winget is not available and bootstrapping is blocked by installer safety mode.'
        Write-WingetUnavailable
        return
    }

    Write-Info 'winget not found; attempting App Installer bootstrap (optional; Git and Python have direct-download fallbacks).'
    try {
        # First, the owner's proven Sandbox winget-enable command with no downloads. In a warm
        # sandbox (framework deps already staged) this alone enables winget.
        if (Register-AppInstaller) {
            $script:Winget = Find-Winget
            Write-Ok "winget available: $script:Winget"
            return
        }
        # Registration failed because the current App Installer bundle's framework dependencies
        # (WindowsAppRuntime 1.8, VCLibs, UI.Xaml) are not staged in a fresh sandbox. Stage them,
        # then retry the exact same registration command.
        Ensure-WindowsAppRuntime18
        if (Register-AppInstaller) {
            $script:Winget = Find-Winget
            Write-Ok "winget available: $script:Winget"
            return
        }
        if (-not (Test-WindowsSandbox)) {
            if (Invoke-WinGetRepair) {
                $script:Winget = Find-Winget
                Write-Ok "winget available: $script:Winget"
                return
            }
        } else {
            Write-Info 'Windows Sandbox detected; skipping Repair-WinGetPackageManager (known 0x80070005 failure) and using direct packages.'
        }
        if (Install-WinGetDirect) {
            $script:Winget = Find-Winget
            Write-Ok "winget available: $script:Winget"
            return
        }
    } catch {
        Write-Warn "winget bootstrap did not complete: $($_.Exception.Message)"
    }

    Write-WingetUnavailable
}

function Resolve-GitExe {
    Update-ProcessPath
    $cmd = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @()
    if ($env:ProgramFiles) { $candidates += (Join-Path $env:ProgramFiles 'Git\cmd\git.exe') }
    if (${env:ProgramFiles(x86)}) { $candidates += (Join-Path ${env:ProgramFiles(x86)} 'Git\cmd\git.exe') }
    if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Git\cmd\git.exe') }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

function Get-GitInstallerUri {
    Assert-NetworkAllowed 'Git for Windows release lookup'
    $arch = Get-AkCpuArch
    $pattern = if ($arch -eq 'arm64') { 'Git-*-arm64.exe' } else { 'Git-*-64-bit.exe' }
    $pinned = if ($arch -eq 'arm64') { $script:PinnedGitInstallerUriArm64 } else { $script:PinnedGitInstallerUri }
    try {
        $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/git-for-windows/git/releases/latest' -Headers @{ 'User-Agent' = 'AgentKaizenInstaller' } -ErrorAction Stop
        $asset = $release.assets | Where-Object { $_.name -like $pattern } | Select-Object -First 1
        if ($asset -and $asset.browser_download_url) { return $asset.browser_download_url }
    } catch {
        Write-Warn "Could not query latest Git for Windows release: $($_.Exception.Message)"
    }
    return $pinned
}

function Install-GitDirect {
    Assert-NetworkAllowed 'direct Git for Windows download'
    Assert-ExternalAllowed 'direct Git for Windows install'
    $downloadRoot = Join-Path $script:WorkRoot 'downloads\git'
    New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
    $uri = Get-GitInstallerUri
    $installerPath = Join-Path $downloadRoot ([IO.Path]::GetFileName(([Uri]$uri).LocalPath))
    Invoke-AkDownload -Uri $uri -OutFile $installerPath -Label 'Git for Windows' -Verify exe -MinimumBytes 20MB
    try {
        Invoke-Native -FilePath $installerPath -ArgumentList @('/VERYSILENT', '/NORESTART', '/NOCANCEL', '/SP-', '/SUPPRESSMSGBOXES') -Network -Note 'Installing Git for Windows by direct download.' | Out-Null
    } catch {
        Write-Warn "Git for Windows installer reported an error; probing for git.exe anyway: $($_.Exception.Message)"
    }
    Update-ProcessPath
    return [bool](Resolve-GitExe)
}

function Ensure-Git {
    $git = Resolve-GitExe
    if (-not $git) {
        if (-not $script:Winget -and (Test-WingetNow)) { $script:Winget = Find-Winget }
        if ($NoNetwork) {
            throw 'Git is not installed and -NoNetwork blocks installing it. Install Git for Windows (https://git-scm.com/download/win) and rerun.'
        }
        if ($script:Winget) {
            try {
                Invoke-Native -FilePath $script:Winget -ArgumentList @('install', '--id', 'Git.Git', '-e', '--source', 'winget', '--accept-package-agreements', '--accept-source-agreements', '--silent') -Network -Note 'Installing Git for Windows via winget.'
                Update-ProcessPath
            } catch {
                Write-Warn "winget install of Git did not complete; falling back to direct download: $($_.Exception.Message)"
            }
            $git = Resolve-GitExe
        }
        if (-not $git) {
            try {
                if (Install-GitDirect) { $git = Resolve-GitExe }
            } catch {
                Write-Warn "Direct Git for Windows install did not complete: $($_.Exception.Message)"
            }
        }
    }
    if (-not $git) {
        Write-Host ''
        Write-Host 'Git could not be installed automatically via winget or direct download.' -ForegroundColor Red
        Write-Host 'Manual install: https://git-scm.com/download/win  (then rerun this installer)'
        Write-Host 'If Git was just installed, open a fresh terminal and rerun.'
        throw 'Git could not be installed via winget or direct download.'
    }
    $script:Git = $git
    Invoke-Native -FilePath $git -ArgumentList @('--version') -Note 'Validate Git for Windows.'
}

function Resolve-RealPython {
    Update-ProcessPath
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($script:PythonBaseDir) { $candidates.Add((Join-Path $script:PythonBaseDir 'python.exe')) }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { $candidates.Add($cmd.Source) }
    if ($env:LOCALAPPDATA) {
        $programs = Join-Path $env:LOCALAPPDATA 'Programs\Python'
        if (Test-Path -LiteralPath $programs) {
            Get-ChildItem -LiteralPath $programs -Filter 'Python3*' -Directory -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                ForEach-Object { $candidates.Add((Join-Path $_.FullName 'python.exe')) }
        }
    }
    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate)) { continue }
        if ($candidate -like '*\WindowsApps\*') { continue }
        try {
            $v = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) {
                $parts = $v.Trim().Split('.')
                if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 12)) {
                    return $candidate
                }
            }
        } catch {}
    }
    return $null
}

function Get-PythonInstallerUri {
    # Official python.org Windows installer, matching the SC installer's approach.
    $v = $script:PinnedPythonVersion
    switch (Get-AkCpuArch) {
        'arm64' { return "https://www.python.org/ftp/python/$v/python-$v-arm64.exe" }
        'x86' { return "https://www.python.org/ftp/python/$v/python-$v.exe" }
        default { return "https://www.python.org/ftp/python/$v/python-$v-amd64.exe" }
    }
}

function Install-PythonDirect {
    Assert-NetworkAllowed 'Python download'
    Assert-ExternalAllowed 'Python install'
    $downloadRoot = Join-Path $script:WorkRoot 'downloads\python'
    New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
    $target = $script:PythonBaseDir
    $py = Join-Path $target 'python.exe'
    $uri = Get-PythonInstallerUri
    $installerPath = Join-Path $downloadRoot ([IO.Path]::GetFileName(([Uri]$uri).LocalPath))
    Invoke-AkDownload -Uri $uri -OutFile $installerPath -Label ("Python {0}" -f $script:PinnedPythonVersion) -Verify exe -MinimumBytes 10MB
    # Install into an explicit DEVROOT target (SC's Install-PythonIntoDevRoot approach): per-user,
    # no admin, PrependPath=0 (we point $script:Python directly and Resolve-RealPython finds it).
    # python.org's .exe is a WiX-burn bootstrapper, so run it through the job-based runner (not
    # Invoke-Native) and pass /log so a sandbox MSI failure (e.g. exit 2147946951) writes a legible
    # diagnostic that AK_LOG_MIRROR ships to the host instead of a 1-second no-op.
    New-Item -ItemType Directory -Path $script:LogRoot -Force | Out-Null
    $installLog = Join-Path $script:LogRoot ('python-install-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    $exitCode = Invoke-AkInstaller -FilePath $installerPath -ArgumentList @('/quiet', '/log', $installLog, 'InstallAllUsers=0', "TargetDir=$target", 'Include_pip=1', 'Include_launcher=1', 'InstallLauncherAllUsers=0', 'PrependPath=0', 'Include_test=0', 'Shortcuts=0') -Network -Note "Installing Python into $target (python.org installer)."
    if ($exitCode -eq 3010) { Write-Info 'Python installer reported reboot-pending (3010); validating on disk.' }
    Update-ProcessPath
    # A detached bootstrapper child can still be finishing after the parent returns; give python.exe
    # up to ~30s to appear before declaring failure.
    if (-not (Test-Path -LiteralPath $py)) {
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 1
            if (Test-Path -LiteralPath $py) { break }
        }
    }
    if (-not (Test-Path -LiteralPath $py)) {
        Write-Warn ("Python installer exit code {0}; python.exe not found at {1}." -f $exitCode, $py)
        Write-Info ("Python install diagnostic log: {0}" -f $installLog)
    }
    return (Test-Path -LiteralPath $py)
}

function Ensure-Python {
    # Python is installed from the python.org Windows installer into an explicit DEVROOT target,
    # exactly like the SC installer. No winget, no nuget.
    $python = Resolve-RealPython
    if (-not $python) {
        if ($NoNetwork) {
            throw 'Python 3.12+ is not installed and -NoNetwork blocks installing it. Install Python 3.12+ (https://www.python.org/downloads/) and rerun.'
        }
        try {
            if (Install-PythonDirect) { $python = Resolve-RealPython }
        } catch {
            Write-Warn "Python install did not complete: $($_.Exception.Message)"
        }
    }
    if (-not $python) {
        Write-Host ''
        Write-Host 'Python 3.12+ could not be installed.' -ForegroundColor Red
        Write-Host 'Manual install: https://www.python.org/downloads/  (tick "Add python.exe to PATH", then rerun)'
        Write-Host 'If Python was just installed, open a fresh terminal and rerun.'
        throw 'Python 3.12+ could not be installed.'
    }
    $script:Python = $python
    Invoke-Native -FilePath $python -ArgumentList @('-c', 'import sys,venv; print(sys.version)') -Note 'Validate Python 3.12+ and venv module.'
}

function Read-AkYesNo {
    param([Parameter(Mandatory)][string] $Prompt, [bool] $Default = $false)
    $suffix = if ($Default) { '[Y/n]' } else { '[y/N]' }
    $ans = (Read-Host ("{0} {1}" -f $Prompt, $suffix)).Trim().ToLowerInvariant()
    if ($ans -eq '') { return $Default }
    return ($ans -eq 'y' -or $ans -eq 'yes')
}

function Select-AkDevTools {
    if ($NoDevTools) {
        Write-Info 'Developer tool installs disabled (-NoDevTools). Turso then needs a prebuilt wheel.'
        return
    }
    # -AssumeYes only auto-picks DEVROOT; it does NOT suppress this menu. Only -NoPrompt / explicit
    # -With* flags make this non-interactive.
    $scripted = $NoPrompt -or $WithRust -or $WithBuildTools -or $WithDotNet -or $WithCMake -or $WithNode -or $WithVSCode

    if ($scripted) {
        if ($WithRust -or $WithBuildTools) {
            # Explicit flags win exactly.
            $script:ToolRust = [bool]$WithRust
            $script:ToolBuildTools = [bool]$WithBuildTools
        } else {
            # Scripted with no explicit Rust/Build-Tools choice -> default ON (Turso must compile).
            $script:ToolRust = $true
            $script:ToolBuildTools = $true
        }
        $script:ToolDotNet = [bool]$WithDotNet
        $script:ToolCMake = [bool]$WithCMake
        $script:ToolNode = [bool]$WithNode
        $script:ToolVSCode = [bool]$WithVSCode
    } else {
        Write-Host ''
        Write-Host 'Turso (the Agent Kaizen database) has no prebuilt Windows package, so pip compiles it'
        Write-Host 'from source. That requires Rust + Visual Studio Build Tools (a multi-GB download; the'
        Write-Host 'first compile takes a few minutes). Skip only if you supply a prebuilt pyturso wheel'
        Write-Host '(drop it in DEVROOT\wheels or pass -PyTursoWheelUrl; see setup/SETUP.md).'
        $rustBt = Read-AkYesNo -Prompt 'Install Rust + VS Build Tools to compile Turso?' -Default $true
        $script:ToolRust = $rustBt
        $script:ToolBuildTools = $rustBt
        Write-Host ''
        Write-Host 'Optional developer tools (installed at the end; default No):'
        $script:ToolVSCode = Read-AkYesNo -Prompt '  Install VS Code?' -Default $false
        $script:ToolDotNet = Read-AkYesNo -Prompt '  Install .NET SDKs 8 + 9?' -Default $false
        $script:ToolCMake = Read-AkYesNo -Prompt '  Install CMake?' -Default $false
        $script:ToolNode = Read-AkYesNo -Prompt '  Install Node.js?' -Default $false
    }

    $selected = New-Object System.Collections.Generic.List[string]
    if ($script:ToolRust) { $selected.Add('Rust') }
    if ($script:ToolBuildTools) { $selected.Add('BuildTools') }
    if ($script:ToolVSCode) { $selected.Add('VSCode') }
    if ($script:ToolDotNet) { $selected.Add('DotNet') }
    if ($script:ToolCMake) { $selected.Add('CMake') }
    if ($script:ToolNode) { $selected.Add('Node') }
    $sel = if ($selected.Count) { ($selected -join ', ') } else { 'none' }
    Write-Ok "Developer tools selected: $sel"
    Add-AkSupportSection 'Developer tool selection'
    Add-AkSupportLine "Selected: $sel"
}

function Add-AkPathEntry {
    param([Parameter(Mandatory)][string] $Dir)
    if (-not (Test-Path -LiteralPath $Dir)) { return }
    if ($env:Path -notlike "*$Dir*") { $env:Path = "$Dir;$env:Path" }
    if (-not $NoUserEnvWrites) {
        try {
            $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
            if ([string]::IsNullOrEmpty($userPath)) { $userPath = '' }
            if (($userPath -split ';') -notcontains $Dir) {
                $joined = if ($userPath) { "$userPath;$Dir" } else { $Dir }
                [Environment]::SetEnvironmentVariable('Path', $joined, 'User')
            }
        } catch {}
    }
}

function Set-AkNpmGlobalPrefix {
    # Point npm's global prefix at a DEVROOT dir and put it on PATH (SC's pattern) so `npm i -g`
    # tools are reachable. Only touches AK-managed Node (guarded on npm.cmd under NodeRoot); never a
    # user's system npm.
    if ([string]::IsNullOrWhiteSpace($script:NpmGlobalDir)) { return }
    New-Item -ItemType Directory -Path $script:NpmGlobalDir -Force | Out-Null
    Add-AkPathEntry $script:NpmGlobalDir
    $npmCmd = Join-Path $script:NodeRoot 'npm.cmd'
    if (Test-Path -LiteralPath $npmCmd) {
        try {
            Invoke-Native -FilePath $npmCmd -ArgumentList @('config', 'set', 'prefix', $script:NpmGlobalDir, '--location=user') -Note 'Set npm global prefix to the DEVROOT npm-global dir.'
        } catch { Write-Warn "npm global prefix config did not apply: $($_.Exception.Message)" }
    }
}

function Resolve-RustCargo {
    Update-ProcessPath
    if ($script:RustRoot) {
        $c = Join-Path $script:RustRoot '.cargo\bin\cargo.exe'
        if (Test-Path -LiteralPath $c) { return $c }
    }
    $cmd = Get-Command cargo.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Ensure-Rust {
    if (-not $script:ToolRust) { Set-AkStepSkipped 'Rust not selected'; return }
    $cargo = Resolve-RustCargo
    if ($cargo) {
        Write-Ok "Rust already available: $cargo"
        # Re-persist PATH + CARGO_HOME/RUSTUP_HOME when this is AK's DEVROOT toolchain, so a fresh
        # terminal keeps working even if a prior run's user-env writes were interrupted/lost. A system
        # cargo is left untouched (its DEVROOT .cargo path won't exist).
        $akCargo = Join-Path $script:RustRoot '.cargo\bin\cargo.exe'
        if (Test-Path -LiteralPath $akCargo) {
            Add-AkPathEntry (Join-Path $script:RustRoot '.cargo\bin')
            if (-not $NoUserEnvWrites) {
                try {
                    [Environment]::SetEnvironmentVariable('CARGO_HOME', (Join-Path $script:RustRoot '.cargo'), 'User')
                    [Environment]::SetEnvironmentVariable('RUSTUP_HOME', (Join-Path $script:RustRoot '.rustup'), 'User')
                } catch {}
            }
        }
        Invoke-Native -FilePath $cargo -ArgumentList @('--version') -Note 'Validate Rust (cargo).'
        return
    }
    if ($NoNetwork) { Write-Warn 'Rust is not installed and -NoNetwork blocks installing it; a Turso source build will fail.'; return }
    Assert-NetworkAllowed 'Rust toolchain download'
    Assert-ExternalAllowed 'Rust toolchain install'
    $env:CARGO_HOME = Join-Path $script:RustRoot '.cargo'
    $env:RUSTUP_HOME = Join-Path $script:RustRoot '.rustup'
    New-Item -ItemType Directory -Path $env:CARGO_HOME -Force | Out-Null
    New-Item -ItemType Directory -Path $env:RUSTUP_HOME -Force | Out-Null
    if (-not $NoUserEnvWrites) {
        try {
            [Environment]::SetEnvironmentVariable('CARGO_HOME', $env:CARGO_HOME, 'User')
            [Environment]::SetEnvironmentVariable('RUSTUP_HOME', $env:RUSTUP_HOME, 'User')
        } catch {}
    }
    # Primary: winget (SC's method). Fallback: rustup-init direct if winget is unavailable.
    $installed = $false
    if ($script:Winget) {
        try {
            Invoke-Native -FilePath $script:Winget -ArgumentList @('install', '--id', 'Rustlang.Rustup', '-e', '--source', 'winget', '--accept-package-agreements', '--accept-source-agreements', '--silent') -Network -Note 'Installing Rust (rustup) via winget.'
            Update-ProcessPath
            $installed = $true
        } catch { Write-Warn "winget install of Rust did not complete; falling back to rustup-init: $($_.Exception.Message)" }
    }
    if (-not (Resolve-RustCargo) -and -not $installed) {
        $downloadRoot = Join-Path $script:WorkRoot 'downloads\rust'
        New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
        $rustupInit = Join-Path $downloadRoot 'rustup-init.exe'
        Invoke-AkDownload -Uri $script:PinnedRustupInitUri -OutFile $rustupInit -Label 'rustup-init' -Verify exe -MinimumBytes 1MB
        Invoke-Native -FilePath $rustupInit -ArgumentList @('-y', '--default-toolchain', 'stable', '--profile', 'minimal', '--no-modify-path') -Network -Note 'Installing Rust (rustup, stable toolchain).'
    }
    Add-AkPathEntry (Join-Path $env:CARGO_HOME 'bin')
    $cargo = Resolve-RustCargo
    if (-not $cargo) { throw 'Rust was installed but cargo.exe is not available in this process. Open a fresh terminal and rerun.' }
    Invoke-Native -FilePath $cargo -ArgumentList @('--version') -Note 'Validate Rust (cargo) after install.'
}

function Get-AkVsWhere {
    if (${env:ProgramFiles(x86)}) {
        $p = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $null
}

function Get-AkVsDevShell {
    $vswhere = Get-AkVsWhere
    if ($vswhere) {
        try {
            $installPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Workload.VCTools -property installationPath 2>$null | Select-Object -First 1
            if ($installPath) {
                $c = Join-Path $installPath 'Common7\Tools\Launch-VsDevShell.ps1'
                if (Test-Path -LiteralPath $c) {
                    # Remember the install root and the DevShell module. We DO NOT dot-source
                    # Launch-VsDevShell.ps1 (it calls exit() on success, which kills a dot-source
                    # caller); instead callers use the Enter-VsDevShell cmdlet from this module.
                    $script:VsInstallPath = $installPath
                    $curr = Join-Path $installPath 'Common7\Tools\Microsoft.VisualStudio.DevShell.dll'
                    $prev = Join-Path $installPath 'Common7\Tools\vsdevshell\Microsoft.VisualStudio.DevShell.dll'
                    $script:VsDevShellModule = if (Test-Path -LiteralPath $prev) { $prev } elseif (Test-Path -LiteralPath $curr) { $curr } else { $null }
                    return $c
                }
            }
        } catch {}
    }
    return $null
}

function Enter-AkVsDevEnv {
    # Enter the VS Developer environment in the CURRENT process using the Enter-VsDevShell cmdlet
    # (what Launch-VsDevShell.ps1 calls internally). The cmdlet sets cl.exe/MSVC/SDK on PATH and
    # RETURNS -- unlike the script, which exit()s. Returns $true on success.
    if (-not $script:VsInstallPath -or -not $script:VsDevShellModule) { return $false }
    if (-not (Test-Path -LiteralPath $script:VsDevShellModule)) { return $false }
    try {
        Import-Module $script:VsDevShellModule -ErrorAction Stop
        Enter-VsDevShell -VsInstallPath $script:VsInstallPath -SkipAutomaticLocation -Arch amd64 -HostArch amd64 *> $null
        return $true
    } catch { return $false }
}

function Ensure-BuildTools {
    if (-not $script:ToolBuildTools) { Set-AkStepSkipped 'VS Build Tools not selected'; return }
    $devshell = Get-AkVsDevShell
    if ($devshell) {
        $script:VsDevShell = $devshell
        Write-Ok "VS Build Tools available. Developer PowerShell: $devshell"
        return
    }
    if ($NoNetwork) { Write-Warn 'VS Build Tools not installed and -NoNetwork blocks installing them; a Turso source build will fail.'; return }
    Assert-NetworkAllowed 'VS Build Tools download'
    Assert-ExternalAllowed 'VS Build Tools install'
    $downloadRoot = Join-Path $script:WorkRoot 'downloads\buildtools'
    New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
    $bootstrapper = Join-Path $downloadRoot 'vs_BuildTools.exe'
    Invoke-AkDownload -Uri $script:PinnedVSBuildToolsUri -OutFile $bootstrapper -Label 'VS Build Tools bootstrapper' -Verify exe -MinimumBytes 1MB
    Write-Info 'Installing VS Build Tools (VCTools + Windows 11 SDK). Multi-GB download; this can take several minutes...'
    try {
        Invoke-Native -FilePath $bootstrapper -ArgumentList @('--passive', '--wait', '--norestart', '--add', 'Microsoft.VisualStudio.Workload.VCTools', '--add', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '--add', 'Microsoft.VisualStudio.Component.Windows11SDK.26100') -Network -Note 'Installing Visual Studio Build Tools.' | Out-Null
    } catch {
        Write-Warn "VS Build Tools installer returned an error (3010 = reboot pending); revalidating anyway: $($_.Exception.Message)"
    }
    # The bootstrapper can return before helper processes finish writing files; re-detect the fresh
    # instance a few times (SC's pattern) instead of failing immediately.
    for ($i = 0; $i -lt 3; $i++) {
        $devshell = Get-AkVsDevShell
        if ($devshell) { break }
        Write-Info ("Bootstrapper returned; re-checking VS Build Tools install ({0}/3)..." -f ($i + 1))
        Start-Sleep -Seconds 10
    }
    if (-not $devshell) {
        throw 'VS Build Tools installed but did not validate (vswhere/VCTools not found). If a reboot was requested, reboot, then relaunch this installer -- it re-checks Build Tools first and will not reinstall if they validate.'
    }
    $script:VsDevShell = $devshell
    Write-Ok "VS Build Tools installed. Developer PowerShell: $devshell"
}

function Ensure-VSCode {
    if (-not $script:ToolVSCode) { Set-AkStepSkipped 'not selected'; return }
    $existing = Get-Command code -ErrorAction SilentlyContinue
    if ($existing) { Write-Ok "VS Code already available: $($existing.Source)"; return }
    $codeCmd = Join-Path $script:VSCodeRoot 'bin\code.cmd'
    if (Test-Path -LiteralPath $codeCmd) { Add-AkPathEntry (Join-Path $script:VSCodeRoot 'bin'); Write-Ok "VS Code already present: $codeCmd"; return }
    if ($NoNetwork) { Write-Warn 'VS Code not installed and -NoNetwork blocks installing it; skipping.'; return }
    Assert-NetworkAllowed 'VS Code download'
    Assert-ExternalAllowed 'VS Code install'
    $downloadRoot = Join-Path $script:WorkRoot 'downloads\vscode'
    New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
    $zip = Join-Path $downloadRoot 'vscode-win32-x64-archive.zip'
    Invoke-AkDownload -Uri $script:PinnedVSCodeArchiveUri -OutFile $zip -Label 'VS Code (archive)' -Verify zip -MinimumBytes 50MB
    if (Test-Path -LiteralPath $script:VSCodeRoot) { Remove-Item -LiteralPath $script:VSCodeRoot -Recurse -Force -ErrorAction SilentlyContinue }
    Expand-Archive -LiteralPath $zip -DestinationPath $script:VSCodeRoot -Force
    Add-AkPathEntry (Join-Path $script:VSCodeRoot 'bin')
    if (Test-Path -LiteralPath $codeCmd) {
        try { Invoke-Native -FilePath $codeCmd -ArgumentList @('--version') -Note 'Validate VS Code.' } catch { Write-Warn "VS Code installed but --version did not run: $($_.Exception.Message)" }
        Write-Ok "VS Code installed: $script:VSCodeRoot"
    } else {
        Write-Warn "VS Code archive extracted but code.cmd was not found under $script:VSCodeRoot."
    }
}

function Ensure-DotNet {
    if (-not $script:ToolDotNet) { Set-AkStepSkipped 'not selected'; return }
    $dotnet = Join-Path $script:DotNetRoot 'dotnet.exe'
    if (Test-Path -LiteralPath $dotnet) { Add-AkPathEntry $script:DotNetRoot; Write-Ok "'.NET' already present: $dotnet"; return }
    if ($NoNetwork) { Write-Warn '.NET not installed and -NoNetwork blocks installing it; skipping.'; return }
    Assert-NetworkAllowed '.NET SDK download'
    Assert-ExternalAllowed '.NET SDK install'
    $downloadRoot = Join-Path $script:WorkRoot 'downloads\dotnet'
    New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
    $installScript = Join-Path $downloadRoot 'dotnet-install.ps1'
    Invoke-AkDownload -Uri $script:PinnedDotNetInstallScriptUri -OutFile $installScript -Label 'dotnet-install.ps1'
    foreach ($channel in @('8.0', '9.0')) {
        Invoke-Native -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $installScript, '-Channel', $channel, '-InstallDir', $script:DotNetRoot, '-NoPath') -Network -Note ("Installing .NET SDK {0}." -f $channel)
    }
    Add-AkPathEntry $script:DotNetRoot
    if (-not $NoUserEnvWrites) { try { [Environment]::SetEnvironmentVariable('DOTNET_ROOT', $script:DotNetRoot, 'User') } catch {} }
    if (Test-Path -LiteralPath $dotnet) {
        try { Invoke-Native -FilePath $dotnet -ArgumentList @('--list-sdks') -Note 'Validate .NET SDKs.' } catch {}
        Write-Ok ".NET SDKs installed: $script:DotNetRoot"
    } else {
        Write-Warn ".NET install completed but dotnet.exe was not found under $script:DotNetRoot."
    }
}

function Ensure-CMake {
    if (-not $script:ToolCMake) { Set-AkStepSkipped 'not selected'; return }
    $existing = Get-Command cmake -ErrorAction SilentlyContinue
    if ($existing) { Add-AkPathEntry (Join-Path $script:CMakeRoot 'bin'); Write-Ok "CMake already available: $($existing.Source)"; return }
    if ($NoNetwork) { Write-Warn 'CMake not installed and -NoNetwork blocks installing it; skipping.'; return }
    Assert-NetworkAllowed 'CMake download'
    Assert-ExternalAllowed 'CMake install'
    $v = $script:PinnedCMakeVersion
    $uri = "https://github.com/Kitware/CMake/releases/download/v$v/cmake-$v-windows-x86_64.zip"
    $downloadRoot = Join-Path $script:WorkRoot 'downloads\cmake'
    New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
    $zip = Join-Path $downloadRoot "cmake-$v.zip"
    Invoke-AkDownload -Uri $uri -OutFile $zip -Label 'CMake' -Verify zip -MinimumBytes 5MB
    $extract = Join-Path $downloadRoot 'extract'
    if (Test-Path -LiteralPath $extract) { Remove-Item -LiteralPath $extract -Recurse -Force -ErrorAction SilentlyContinue }
    Expand-Archive -LiteralPath $zip -DestinationPath $extract -Force
    $binDir = (Get-ChildItem -LiteralPath $extract -Recurse -Filter 'cmake.exe' -File -ErrorAction SilentlyContinue | Select-Object -First 1).DirectoryName
    if (-not $binDir) { throw "cmake.exe not found in the extracted CMake archive." }
    if (Test-Path -LiteralPath $script:CMakeRoot) { Remove-Item -LiteralPath $script:CMakeRoot -Recurse -Force -ErrorAction SilentlyContinue }
    Move-Item -LiteralPath (Split-Path -Parent $binDir) -Destination $script:CMakeRoot -Force
    Add-AkPathEntry (Join-Path $script:CMakeRoot 'bin')
    Write-Ok "CMake installed: $script:CMakeRoot"
}

function Ensure-Node {
    if (-not $script:ToolNode) { Set-AkStepSkipped 'not selected'; return }
    $existing = Get-Command node -ErrorAction SilentlyContinue
    if ($existing) { Add-AkPathEntry $script:NodeRoot; Add-AkPathEntry $script:NpmGlobalDir; Write-Ok "Node.js already available: $($existing.Source)"; return }
    if ($NoNetwork) { Write-Warn 'Node.js not installed and -NoNetwork blocks installing it; skipping.'; return }
    Assert-NetworkAllowed 'Node.js download'
    Assert-ExternalAllowed 'Node.js install'
    $v = $script:PinnedNodeVersion
    $uri = "https://nodejs.org/dist/v$v/node-v$v-win-x64.zip"
    $downloadRoot = Join-Path $script:WorkRoot 'downloads\node'
    New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
    $zip = Join-Path $downloadRoot "node-$v.zip"
    Invoke-AkDownload -Uri $uri -OutFile $zip -Label 'Node.js' -Verify zip -MinimumBytes 5MB
    $extract = Join-Path $downloadRoot 'extract'
    if (Test-Path -LiteralPath $extract) { Remove-Item -LiteralPath $extract -Recurse -Force -ErrorAction SilentlyContinue }
    Expand-Archive -LiteralPath $zip -DestinationPath $extract -Force
    $nodeDir = (Get-ChildItem -LiteralPath $extract -Recurse -Filter 'node.exe' -File -ErrorAction SilentlyContinue | Select-Object -First 1).DirectoryName
    if (-not $nodeDir) { throw "node.exe not found in the extracted Node.js archive." }
    if (Test-Path -LiteralPath $script:NodeRoot) { Remove-Item -LiteralPath $script:NodeRoot -Recurse -Force -ErrorAction SilentlyContinue }
    Move-Item -LiteralPath $nodeDir -Destination $script:NodeRoot -Force
    Add-AkPathEntry $script:NodeRoot
    Set-AkNpmGlobalPrefix
    Write-Ok "Node.js installed: $script:NodeRoot"
}

function Test-NetworkRepoSource {
    param([Parameter(Mandatory)][string] $Source)
    return ($Source -match '^(https?|ssh|git)://' -or $Source -match '^[^@]+@[^:]+:')
}

function Step-DevRoot {
    # Verify pillar: only create folders that are missing and only write DEVROOT when it differs.
    foreach ($d in @($script:DevRoot, $script:WorkRoot, $script:LogRoot)) {
        if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    }
    $env:DEVROOT = $script:DevRoot
    if ($NoPersistDevRoot -or $NoUserEnvWrites) {
        Write-Info 'Skipping persisted DEVROOT user environment write.'
    } elseif ([Environment]::GetEnvironmentVariable('DEVROOT', 'User') -eq $script:DevRoot) {
        Write-Ok "DEVROOT already persisted for this user: $script:DevRoot"
    } else {
        Assert-UserEnvWritesAllowed 'persist DEVROOT user environment variable'
        [Environment]::SetEnvironmentVariable('DEVROOT', $script:DevRoot, 'User')
        Write-Ok "Persisted DEVROOT for this user: $script:DevRoot"
    }
    Write-Ok "Setup logs: $script:LogRoot"
}

function Test-WindowsSandbox {
    try {
        $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        if ($user -match 'WDAGUtilityAccount') { return $true }
    } catch {}
    try {
        $cs = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop
        if ($cs.Model -match 'Virtual Machine' -and $env:USERPROFILE -match 'WDAGUtilityAccount') { return $true }
    } catch {}
    return $false
}

function Get-AkSmartAppControlState {
    # 0 = Off, 1 = Enforce, 2 = Evaluation. When On/Evaluation, Windows may block unsigned
    # helper DLLs (e.g. Git's MSYS runtime) with a "Bad Image" dialog. There is no programmatic
    # bypass; core signed tools (git.exe, python.exe) still install and run.
    try {
        $raw = (Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -Name 'VerifiedAndReputablePolicyState' -ErrorAction Stop).VerifiedAndReputablePolicyState
        switch ([int]$raw) {
            0 { return 'Off' }
            1 { return 'Enforce' }
            2 { return 'Evaluation' }
            default { return 'Unknown' }
        }
    } catch {
        return 'Unknown'
    }
}

function Step-Preflight {
    $self = $env:AK_SELF
    $sourceDir = if ($self) { Split-Path -Parent $self } else { '<unknown>' }
    Write-Info "Installer source: $sourceDir"
    Write-Info "Windows Sandbox detected: $(if (Test-WindowsSandbox) { 'yes' } else { 'no' })"
    Write-Info "NoNetwork=$([bool]$NoNetwork); NoExternalActions=$([bool]$NoExternalActions); NoUserEnvWrites=$([bool]$NoUserEnvWrites); NoPrompt=$([bool]$NoPrompt)"
    $sac = Get-AkSmartAppControlState
    $script:SmartAppControl = $sac
    Write-Info "Smart App Control: $sac"
    if ($sac -eq 'Enforce' -or $sac -eq 'Evaluation') {
        Write-Warn "Smart App Control is $sac. It can block unsigned/low-reputation binaries and installer engines, so Git may show a `"Bad Image`" dialog and the Python install may not complete."
        Write-Info 'There is no programmatic bypass. If a tool install fails under Smart App Control,'
        Write-Info 'turn it off in Windows Security > App & browser control > Smart App Control settings, then rerun this installer.'
        Add-AkSupportSection 'Smart App Control'
        Add-AkSupportLine "State: $sac (can block unsigned DLLs and installer engines; may cause tool-install failures)"
    }
    if ($RepoSource -and (Test-Path -LiteralPath $RepoSource)) {
        $attrs = (Get-Item -LiteralPath $RepoSource -Force).Attributes
        if (($attrs -band [IO.FileAttributes]::ReadOnly) -ne 0) {
            Write-Warn 'RepoSource is marked read-only; installer will clone from it and write only under DEVROOT.'
        }
    }
    if ($script:DevRoot -match '^[A-Za-z]:\\?$') {
        throw 'DEVROOT must be a folder path, not only a drive root.'
    }
}

function Step-Repo {
    $source = if ($RepoSource) { $RepoSource } else { $RepoUrl }
    if (Test-Path -LiteralPath (Join-Path $script:RepoPath '.git')) {
        Write-Info "Repository already exists: $script:RepoPath"
        if (Test-NetworkRepoSource $source) {
            Invoke-Native -FilePath $script:Git -ArgumentList @('-C', $script:RepoPath, 'fetch', '--all', '--tags', '--prune') -Network -Note 'Refresh existing repository.'
        }
        if ($Ref) {
            Invoke-Native -FilePath $script:Git -ArgumentList @('-C', $script:RepoPath, 'checkout', $Ref) -Note 'Check out requested Agent Kaizen ref.'
        } elseif (Test-NetworkRepoSource $source) {
            Invoke-Native -FilePath $script:Git -ArgumentList @('-C', $script:RepoPath, 'pull', '--ff-only') -Network -Note 'Fast-forward existing repository.'
        }
        return
    }
    if (Test-Path -LiteralPath $script:RepoPath) {
        throw "Target repository folder exists but is not a git repository: $script:RepoPath"
    }
    $args = @('clone', $source, $script:RepoPath)
    Invoke-Native -FilePath $script:Git -ArgumentList $args -Network:(Test-NetworkRepoSource $source) -Note 'Clone Agent Kaizen repository.'
    if ($Ref) {
        Invoke-Native -FilePath $script:Git -ArgumentList @('-C', $script:RepoPath, 'checkout', $Ref) -Note 'Check out requested Agent Kaizen ref.'
    }
}

function Step-Venv {
    if (-not (Test-Path -LiteralPath $script:VenvPython)) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $script:VenvRoot) -Force | Out-Null
        Invoke-Native -FilePath $script:Python -ArgumentList @('-m', 'venv', $script:VenvRoot) -Note 'Create shared Agent Kaizen Python venv.'
    } else {
        Write-Ok "Shared venv already exists: $script:VenvRoot"
    }
    if (-not (Test-Path -LiteralPath $script:VenvPython)) {
        throw "Venv Python was not created: $script:VenvPython"
    }
    Invoke-Native -FilePath $script:VenvPython -ArgumentList @('-c', 'import sys; print(sys.executable)') -Note 'Validate shared venv Python.'
}

function Step-Dependencies {
    $requirements = Join-Path $script:RepoPath 'requirements-kaizen.txt'
    if (-not (Test-Path -LiteralPath $requirements)) {
        throw "Missing requirements file: $requirements"
    }
    # Pre-flight (Verify pillar): pyturso importing in the shared venv is this step's acceptance test.
    # If it already imports, deps are satisfied -- skip the pip resolve and any pyturso rebuild.
    if ($script:VenvPython -and (Test-Path -LiteralPath $script:VenvPython)) {
        try {
            & $script:VenvPython -c 'import pyturso' 2>$null
            if ($LASTEXITCODE -eq 0) { Write-Ok 'Python dependencies already satisfied (pyturso imports); skipping.'; return }
        } catch {}
    }
    Invoke-Native -FilePath $script:VenvPython -ArgumentList @('-m', 'pip', 'install', '--upgrade', 'pip') -Network -WorkingDirectory $script:RepoPath -Note 'Upgrade pip in shared venv.'

    # Wheel-first: pyturso has no Windows wheel on PyPI, so pip must build it from source unless a
    # prebuilt wheel is provided. Collect local wheel sources for --find-links.
    New-Item -ItemType Directory -Path $script:WheelCache -Force | Out-Null
    $wheelDirs = New-Object System.Collections.Generic.List[string]
    $wheelDirs.Add($script:WheelCache)
    $repoWheels = Join-Path $script:RepoPath 'wheels'
    if (Test-Path -LiteralPath $repoWheels) { $wheelDirs.Add($repoWheels) }
    if ($PyTursoWheelUrl -and -not $NoNetwork) {
        try {
            $whl = Join-Path $script:WheelCache ([IO.Path]::GetFileName(([Uri]$PyTursoWheelUrl).LocalPath))
            Invoke-AkDownload -Uri $PyTursoWheelUrl -OutFile $whl -Label 'pyturso wheel' -Verify zip -MinimumBytes 100KB
        } catch { Write-Warn "Could not download the pyturso wheel from -PyTursoWheelUrl: $($_.Exception.Message)" }
    }
    $haveWheel = $false
    foreach ($d in $wheelDirs) {
        if ((Test-Path -LiteralPath $d) -and (Get-ChildItem -LiteralPath $d -Filter 'pyturso-*.whl' -File -ErrorAction SilentlyContinue | Select-Object -First 1)) { $haveWheel = $true; break }
    }

    if (-not $haveWheel) {
        # Building from source needs Rust + a C/C++ toolchain.
        if (-not ($script:ToolRust -and $script:ToolBuildTools)) {
            Write-Host ''
            Write-Host 'pyturso has no prebuilt Windows wheel, so it must be compiled from source, but Rust +' -ForegroundColor Red
            Write-Host 'VS Build Tools were not installed.' -ForegroundColor Red
            Write-Host 'Rerun and choose to install Rust + Build Tools, or place a prebuilt pyturso wheel in:'
            Write-Host ("  {0}" -f $script:WheelCache)
            Write-Host '(or pass -PyTursoWheelUrl <url>). See setup/SETUP.md.'
            throw 'Cannot install pyturso: no prebuilt wheel and no build toolchain selected.'
        }
        # Enter the VS Developer environment so cl.exe / link.exe / the Windows SDK are on PATH,
        # and make sure cargo is reachable, before pip drives the maturin build.
        if ($script:VsInstallPath) {
            Write-Info 'Entering the Visual Studio Developer environment for the native build...'
            if (-not (Enter-AkVsDevEnv)) {
                Write-Warn 'Could not enter the VS Developer environment; the native build may fail.'
            }
        }
        if ($script:RustRoot) { Add-AkPathEntry (Join-Path $script:RustRoot '.cargo\bin') }
    }

    $pipArgs = New-Object System.Collections.Generic.List[string]
    $pipArgs.AddRange([string[]]@('-m', 'pip', 'install', '--prefer-binary'))
    foreach ($d in $wheelDirs) { $pipArgs.Add('--find-links'); $pipArgs.Add($d) }
    $pipArgs.Add('-r'); $pipArgs.Add($requirements)
    $note = if ($haveWheel) { 'Install Agent Kaizen Python dependencies (pyturso from prebuilt wheel).' } else { 'Install Agent Kaizen Python dependencies (compiling pyturso from source).' }
    Invoke-Native -FilePath $script:VenvPython -ArgumentList $pipArgs.ToArray() -Network -WorkingDirectory $script:RepoPath -Note $note

    if (-not $haveWheel) {
        # Cache the freshly built wheel so later runs / machines can skip the toolchain.
        try {
            Invoke-Native -FilePath $script:VenvPython -ArgumentList @('-m', 'pip', 'wheel', 'pyturso==0.6.1', '-w', $script:WheelCache) -Network -WorkingDirectory $script:RepoPath -Note 'Cache the built pyturso wheel for reuse.'
        } catch { Write-Warn "Could not cache the built pyturso wheel: $($_.Exception.Message)" }
    }

    # Import smoke check. This is the validation point for whether the native module runs here
    # (e.g. under Smart App Control Enforce), NOT a pre-judgment.
    try {
        Invoke-Native -FilePath $script:VenvPython -ArgumentList @('-c', 'import pyturso; print("pyturso import OK")') -WorkingDirectory $script:RepoPath -Note 'Verify pyturso imports.'
    } catch {
        Write-Host ''
        Write-Host 'pyturso installed but failed to import.' -ForegroundColor Red
        if ($script:SmartAppControl -eq 'Enforce' -or $script:SmartAppControl -eq 'Evaluation') {
            Write-Host "Smart App Control is $script:SmartAppControl and may be blocking the native module (.pyd)." -ForegroundColor Red
            Write-Host 'The mirrored logs capture the exact error for follow-up.'
        }
        throw
    }
}

function Get-AkAvailableSkills {
    # Live-enumerate the owner's published skill repos: LevyBytes/AI-skill-drafting + AI-SKILL-*.
    # Map each repo to its skill folder name (strip the AI-SKILL- / AI- prefix): AI-SKILL-git -> git,
    # AI-skill-drafting -> skill-drafting. -creplace is case-sensitive so the two forms do not collide.
    Assert-NetworkAllowed 'GitHub skill repo enumeration'
    $repos = Invoke-RestMethod -Uri 'https://api.github.com/users/LevyBytes/repos?per_page=100&type=public' -Headers @{ 'User-Agent' = 'AgentKaizenInstaller'; 'Accept' = 'application/vnd.github+json' } -ErrorAction Stop
    $skills = New-Object System.Collections.Generic.List[object]
    foreach ($r in $repos) {
        $rn = [string]$r.name
        if ($rn -cmatch '^AI-SKILL-' -or $rn -cmatch '^AI-skill-') {
            $name = $rn -creplace '^AI-SKILL-', '' -creplace '^AI-skill-', 'skill-'
            $skills.Add([pscustomobject]@{ Repo = $rn; Name = $name; CloneUrl = [string]$r.clone_url })
        }
    }
    return @($skills | Sort-Object Name)
}

function Ensure-Skills {
    # Optional, best-effort: never throw out of this step. Ask which published skills to install
    # (skill-drafting always), clone each into DEVROOT\SKILLS\skills\<name>, and junction it into
    # .agents\skills + .claude\skills. Headless/-NoPrompt installs skill-drafting only.
    try {
        $store = Join-Path $script:DevRoot 'SKILLS'
        $skillsDir = Join-Path $store 'skills'
        New-Item -ItemType Directory -Path $skillsDir -Force | Out-Null

        if ($NoNetwork -or $NoExternalActions) {
            Write-Warn 'Skills download is blocked by installer safety mode; skipping (run setup/link-skills later).'
            return
        }
        $git = Resolve-GitExe
        if (-not $git) { Write-Warn 'git not found; skipping skills install.'; return }

        $available = @()
        try { $available = @(Get-AkAvailableSkills) } catch { Write-Warn "Could not list skills from GitHub: $($_.Exception.Message)" }
        if (-not $available -or $available.Count -eq 0) { Write-Warn 'No published skills found; skipping.'; return }

        # skill-drafting is always installed; the rest come from the prompt (or default headless).
        $selected = New-Object System.Collections.Generic.List[object]
        $drafting = $available | Where-Object { $_.Name -eq 'skill-drafting' } | Select-Object -First 1
        if ($drafting) { $selected.Add($drafting) }
        $optional = @($available | Where-Object { $_.Name -ne 'skill-drafting' })

        if ((-not $NoPrompt) -and [Environment]::UserInteractive) {
            Write-Host ''
            Write-Host 'Agent Kaizen skills (skill-drafting is always installed):'
            for ($i = 0; $i -lt $optional.Count; $i++) { Write-Host ("  {0,2}) {1}" -f ($i + 1), $optional[$i].Name) }
            Write-Host ''
            $answer = ([string](Read-Host "Enter numbers to add (comma-separated), 'all', or Enter for skill-drafting only")).Trim()
            if ($answer -eq 'all') {
                foreach ($o in $optional) { $selected.Add($o) }
            } elseif ($answer) {
                foreach ($tok in ($answer -split '[,\s]+')) {
                    if ($tok -match '^\d+$') {
                        $idx = [int]$tok - 1
                        if ($idx -ge 0 -and $idx -lt $optional.Count) { $selected.Add($optional[$idx]) }
                    }
                }
            }
        } else {
            Write-Info 'Non-interactive mode: installing skill-drafting only.'
        }

        $mirrors = @((Join-Path $script:RepoPath '.agents\skills'), (Join-Path $script:RepoPath '.claude\skills'))
        foreach ($m in $mirrors) { New-Item -ItemType Directory -Path $m -Force | Out-Null }
        $linked = 0
        $changed = $false
        foreach ($skill in ($selected | Sort-Object Name -Unique)) {
            $dest = Join-Path $skillsDir $skill.Name
            try {
                if (Test-Path -LiteralPath (Join-Path $dest '.git')) {
                    Invoke-Native -FilePath $git -ArgumentList @('-C', $dest, 'pull', '--ff-only') -Network -Note ("Updating skill {0}." -f $skill.Name) | Out-Null
                } elseif (-not (Test-Path -LiteralPath $dest)) {
                    Invoke-Native -FilePath $git -ArgumentList @('clone', '--depth', '1', $skill.CloneUrl, $dest) -Network -Note ("Cloning skill {0}." -f $skill.Name) | Out-Null
                    $changed = $true
                }
                if (-not (Test-Path -LiteralPath (Join-Path $dest 'SKILL.md'))) { Write-Warn ("Skill {0} has no SKILL.md; not linking." -f $skill.Name); continue }
                foreach ($m in $mirrors) {
                    $link = Join-Path $m $skill.Name
                    if (-not (Test-Path -LiteralPath $link)) { New-Item -ItemType Junction -Path $link -Target $dest | Out-Null; $changed = $true }
                }
                $linked += 1
                Write-Ok ("skill linked: {0}" -f $skill.Name)
            } catch {
                Write-Warn ("Skill {0} failed: {1}" -f $skill.Name, $_.Exception.Message)
            }
        }
        Write-Ok ("{0} skill(s) installed and linked into .agents\skills and .claude\skills." -f $linked)

        # Regenerate the skill index only when something was newly cloned/linked (or the index is
        # missing) -- Verify pillar: a warm re-run with no new skills skips this.
        $builder = Join-Path $skillsDir 'skill-drafting\scripts\skill_builder.py'
        $indexMd = Join-Path $script:RepoPath '.claude\skills\INDEX.md'
        if ($changed -or -not (Test-Path -LiteralPath $indexMd)) {
            if ((Test-Path -LiteralPath $builder) -and $script:VenvPython -and (Test-Path -LiteralPath $script:VenvPython)) {
                try {
                    Invoke-Native -FilePath $script:VenvPython -ArgumentList @($builder, 'index', (Join-Path $script:RepoPath '.claude\skills'), '--mirror', (Join-Path $script:RepoPath '.agents\skills')) -WorkingDirectory $script:RepoPath -Note 'Regenerating skill index.' | Out-Null
                } catch { Write-Warn "Skill index regeneration skipped: $($_.Exception.Message)" }
            }
        } else {
            Write-Info 'Skill index unchanged; skipping regeneration.'
        }
    } catch {
        Write-Warn "Skills step did not complete (optional; run setup/link-skills later): $($_.Exception.Message)"
    }
}

function Step-Workspace {
    $workspaceDir = Join-Path $script:RepoPath '_workspace'
    New-Item -ItemType Directory -Path $workspaceDir -Force | Out-Null

    # This step runs AFTER Ensure-BuildTools, so the "developer PowerShell" install is done and the
    # VS instance is known. Fill the workspace profile's __AK_DEVSHELL_PREFIX__ placeholder: enter the
    # VS Dev env (cl.exe / MSVC on PATH) when VS is present, or leave venv-only (graceful) when it is
    # not. Paths are JSON-escaped (\\) so the .code-workspace stays valid JSON.
    # Enter the VS Dev env via the Enter-VsDevShell cmdlet (NOT Launch-VsDevShell.ps1, which exit()s
    # and would abort the terminal before the venv activates). Paths are JSON-escaped (\\) so the
    # .code-workspace stays valid JSON.
    $hasVs = ($script:VsInstallPath -and $script:VsDevShellModule -and (Test-Path -LiteralPath $script:VsDevShellModule))
    $devShellPrefix = ''
    if ($hasVs) {
        $dllJson  = $script:VsDevShellModule.Replace('\', '\\')
        $rootJson = $script:VsInstallPath.Replace('\', '\\')
        $devShellPrefix = "Import-Module '$dllJson'; Enter-VsDevShell -VsInstallPath '$rootJson' -SkipAutomaticLocation -Arch amd64 -HostArch amd64; "
    }
    $workspaceContent = $WorkspaceJson.Replace('__AK_DEVSHELL_PREFIX__', $devShellPrefix)
    Write-Utf8IfChanged -Path (Join-Path $workspaceDir 'agent-kaizen-tools.code-workspace') -Content ($workspaceContent + [Environment]::NewLine)
    Write-Utf8IfChanged -Path (Join-Path $script:RepoPath 'open-agent-kaizen-vscode.cmd') -Content ($LauncherCmd + [Environment]::NewLine)
    New-Item -ItemType Directory -Path (Join-Path $script:DevRoot 'SKILLS\skills') -Force | Out-Null
    # When VS Build Tools were installed, drop a Developer PowerShell launcher that enters the VS
    # dev environment (cl.exe / MSVC on PATH) and activates the shared venv.
    if ($hasVs) {
        $activate = Join-Path $script:VenvRoot 'Scripts\Activate.ps1'
        $devShellCmd = @"
@echo off
setlocal
powershell.exe -NoExit -NoProfile -ExecutionPolicy Bypass -Command "Import-Module '$($script:VsDevShellModule)'; Enter-VsDevShell -VsInstallPath '$($script:VsInstallPath)' -SkipAutomaticLocation -Arch amd64 -HostArch amd64; if (Test-Path '$activate') { & '$activate' }; Set-Location '$($script:RepoPath)'"
endlocal
"@
        Write-Utf8IfChanged -Path (Join-Path $script:RepoPath 'open-agent-kaizen-devshell.cmd') -Content ($devShellCmd + [Environment]::NewLine)
        Write-Ok 'Developer PowerShell launcher written: open-agent-kaizen-devshell.cmd'
    }
    Write-Ok 'Workspace, launcher, and SKILLS folder are present.'
}

function Step-Health {
    $kaizen = Join-Path $script:RepoPath 'kaizen.py'
    if (-not (Test-Path -LiteralPath $kaizen)) {
        throw "Missing kaizen.py: $kaizen"
    }
    Invoke-Native -FilePath $script:VenvPython -ArgumentList @($kaizen, 'K1', '--json') -WorkingDirectory $script:RepoPath -Note 'Initialize/validate Agent Kaizen DB.'
    Invoke-Native -FilePath $script:VenvPython -ArgumentList @($kaizen, 'X5', '--json') -WorkingDirectory $script:RepoPath -Note 'Load private policy context if present.'
    Invoke-Native -FilePath $script:VenvPython -ArgumentList @($kaizen, 'R0', '--json') -WorkingDirectory $script:RepoPath -Note 'Load session digest.'
}

function Pause-IfNeeded {
    if ($ListSteps -or $PlanOnly -or $SelfTest) { return }
    if (-not $NoPause -and [Environment]::UserInteractive) {
        Write-Host ''
        Read-Host 'Press Enter to close' | Out-Null
    }
}

$akExitCode = 0
try {
    Initialize-AkContext

    if ($ListSteps -or $PlanOnly) {
        Show-AkPlan
        if ($EmitPlanJson) { Write-AkPlanJson -Path $EmitPlanJson }
        if ($PlanOnly) { Write-Host ''; Write-Ok 'Plan-only complete; no install actions were run.' }
    } elseif ($SelfTest) {
        Write-Host '=== Agent Kaizen installer self-test ==='
        Write-Host 'No installers, downloads, repo actions, user environment writes, or GUI launches will run.'
        foreach ($step in $script:Steps) {
            Invoke-AkStep -Id $step.Id -Name $step.Name -Body { }
        }
        Write-Host ''
        Write-Ok 'Self-test complete; step shape validated without install actions.'
    } else {
        if ($EmitPlanJson) { Write-AkPlanJson -Path $EmitPlanJson }

        Write-Host '=== Agent Kaizen installer ==='
        Write-Host ("DEVROOT: {0}" -f $script:DevRoot)
        Write-Host ("Logs   : {0}" -f $script:LogRoot)

        Initialize-AkBanner

        Invoke-AkStep -Id 'devroot' -Name (Get-AkStepName 'devroot') -Body ${function:Step-DevRoot}
        Start-AkTranscriptAndEnvironment

        Invoke-AkStep -Id 'preflight' -Name (Get-AkStepName 'preflight') -Body ${function:Step-Preflight}
        Invoke-AkStep -Id 'toolselect' -Name (Get-AkStepName 'toolselect') -Body { Select-AkDevTools }
        Invoke-AkStep -Id 'winget' -Name (Get-AkStepName 'winget') -Body { Ensure-Winget }
        Invoke-AkStep -Id 'git' -Name (Get-AkStepName 'git') -Body { Ensure-Git }
        Invoke-AkStep -Id 'python' -Name (Get-AkStepName 'python') -Body { Ensure-Python }
        Invoke-AkStep -Id 'rust' -Name (Get-AkStepName 'rust') -Body { Ensure-Rust }
        Invoke-AkStep -Id 'buildtools' -Name (Get-AkStepName 'buildtools') -Body { Ensure-BuildTools }
        Invoke-AkStep -Id 'repo' -Name (Get-AkStepName 'repo') -Body ${function:Step-Repo}
        Invoke-AkStep -Id 'venv' -Name (Get-AkStepName 'venv') -Body ${function:Step-Venv}
        Invoke-AkStep -Id 'deps' -Name (Get-AkStepName 'deps') -Body ${function:Step-Dependencies}
        Invoke-AkStep -Id 'workspace' -Name (Get-AkStepName 'workspace') -Body ${function:Step-Workspace}
        Invoke-AkStep -Id 'skills' -Name (Get-AkStepName 'skills') -Body { Ensure-Skills }
        Invoke-AkStep -Id 'health' -Name (Get-AkStepName 'health') -Body ${function:Step-Health}
        Invoke-AkStep -Id 'dotnet' -Name (Get-AkStepName 'dotnet') -Body { Ensure-DotNet }
        Invoke-AkStep -Id 'cmake' -Name (Get-AkStepName 'cmake') -Body { Ensure-CMake }
        Invoke-AkStep -Id 'node' -Name (Get-AkStepName 'node') -Body { Ensure-Node }
        Invoke-AkStep -Id 'vscode' -Name (Get-AkStepName 'vscode') -Body { Ensure-VSCode }

        Write-AkBanner -Completed $script:Steps.Count -StepNo $script:Steps.Count -Status 'done' -Name 'Complete' -Activity ''
        Write-Host ''
        Write-Host 'Agent Kaizen setup complete.' -ForegroundColor Green
        Write-Host ("Repository : {0}" -f $script:RepoPath)
        Write-Host ("Venv       : {0}" -f $script:VenvRoot)
        Write-Host ("Logs       : {0}" -f $script:LogRoot)
        if (-not [string]::IsNullOrWhiteSpace($script:TranscriptLogPath)) { Write-Host ("Transcript : {0}" -f $script:TranscriptLogPath) }
        if (-not [string]::IsNullOrWhiteSpace($script:SupportBundlePath)) { Write-Host ("Support    : {0}" -f $script:SupportBundlePath) }
        Write-Host ("Open VS Code with: {0}" -f (Join-Path $script:RepoPath 'open-agent-kaizen-vscode.cmd'))
    }
} catch {
    $akExitCode = 1
    Write-AkFailureReport -ErrorRecord $_
    Write-Host 'Nothing was force-overwritten. Fix the issue above and rerun; the installer is safe to repeat.'
} finally {
    Stop-AkTranscript
    Copy-AkLogMirror
}

Pause-IfNeeded

# The cmd launcher extracts this payload to a temp file and does not clean it up (the elevated
# window runs detached). Delete our own temp copy on the way out; guard on the launcher's naming
# pattern so running this script directly from a checkout never deletes a real file.
try {
    if ($PSCommandPath -and ($PSCommandPath -like '*Agent-Kaizen-Setup-*.ps1') -and (Test-Path -LiteralPath $PSCommandPath)) {
        Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
    }
} catch {}

exit $akExitCode
