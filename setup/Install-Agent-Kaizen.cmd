@echo off
REM ============================================================================
REM  Install-Agent-Kaizen.cmd  --  self-contained Windows installer
REM ----------------------------------------------------------------------------
REM  Download this ONE file and double-click it (or run it from a terminal).
REM  On a bare machine it installs the prerequisites for you (git + Python, via
REM  winget, user scope - no admin), clones the repo, and builds the local
REM  harness. Re-running is safe; existing pieces are detected and skipped.
REM
REM  Usage:
REM    Install-Agent-Kaizen.cmd [DEVROOT] [-NoPrompt] [-NoPause]
REM    Install-Agent-Kaizen.cmd D:\dev -NoPause
REM
REM  Public repo: https://github.com/LevyBytes/agent-kaizen
REM
REM  How it works: this file is a batch script with an embedded PowerShell
REM  payload after the __AK_PS_PAYLOAD__ marker. The batch half extracts the
REM  payload to a temp file, runs it, then deletes it. The batch half exits
REM  before the marker, so the payload lines never run as batch commands.
REM ============================================================================
setlocal EnableExtensions DisableDelayedExpansion
title Agent Kaizen Setup

where powershell.exe >nul 2>nul
if errorlevel 1 (
  echo Windows PowerShell 5.1+ is required but was not found on PATH.
  echo It ships with Windows 10 and 11. Install/enable it, then run this again.
  pause
  exit /b 1
)

echo Preparing Agent Kaizen setup...

set "AK_SELF=%~f0"
set "AK_TMP=%TEMP%\ak-setup-%RANDOM%%RANDOM%.ps1"
if not exist "%TEMP%\" set "AK_TMP=%LOCALAPPDATA%\ak-setup-%RANDOM%%RANDOM%.ps1"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $m='__AK_PS_PAYLOAD__'; $raw=[IO.File]::ReadAllText($env:AK_SELF); $i=$raw.LastIndexOf($m); if($i -lt 0){[Console]::Error.WriteLine('Installer payload marker not found.'); exit 9}; $p=$raw.Substring($i+$m.Length); [IO.File]::WriteAllText($env:AK_TMP,$p,(New-Object System.Text.UTF8Encoding($false)))"
if errorlevel 1 (
  echo Failed to prepare the installer payload.
  if exist "%AK_TMP%" del "%AK_TMP%" >nul 2>nul
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%AK_TMP%" %*
set "RC=%ERRORLEVEL%"
if exist "%AK_TMP%" del "%AK_TMP%" >nul 2>nul
endlocal & exit /b %RC%

__AK_PS_PAYLOAD__
<#
  Agent Kaizen - embedded PowerShell installer payload.

  Takes a bare Windows machine from nothing to a working harness, in this order:
    1. Prerequisites      install git + Python (>=3.10) via winget if missing
    2. DEVROOT            choose the parent dev folder and create it
    3. Clone             git clone into <DEVROOT>\<repo-name>
    4. Shared venv        <DEVROOT>\Python\venvs\kaizen
    5. Dependencies       pip install -r requirements-kaizen.txt
    6. VS Code artifacts  _workspace\*.code-workspace + open-<name>-vscode.cmd
    7. Skills store       scaffold an empty sibling <DEVROOT>\SKILLS\skills
    8. Local DB           kaizen.py K1 (init only - NO policy/guardrail seeding)
    9. Health check       verify and print a summary

  The test hooks (-RepoSource / -NoPersistDevRoot) let the installer be dry-run
  against a scratch DEVROOT without touching the real environment.
#>
param(
    [string] $DevRoot,
    [string] $RepoName = 'agent-kaizen',
    [string] $RepoUrl  = 'https://github.com/LevyBytes/agent-kaizen.git',
    [string] $RepoSource,            # local path/URL to clone from instead of RepoUrl (testing)
    [switch] $NoPersistDevRoot,      # set the process DEVROOT only; skip setx (testing)
    [switch] $NoPrompt,              # never prompt; use the default/arg DEVROOT (automation)
    [switch] $NoPause
)

$ErrorActionPreference = 'Stop'
$script:Step = 0
$script:TotalSteps = 9

# --- The VS Code workspace JSON the installer writes. Every path is relative to
#     this file's folder (<repo>\_workspace), so it is independent of the chosen
#     repo folder name. ---------------------------------------------------------
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
          "$projectRoot=(Resolve-Path '${workspaceFolder:Agent Kaizen}').Path; $pythonActivate='${workspaceFolder:Agent Kaizen}/../Python/venvs/kaizen/Scripts/Activate.ps1'; if (Test-Path $pythonActivate) { & $pythonActivate } else { Write-Warning 'Python venv not found. Run the setup script.' }; Set-Location $projectRoot"
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

# --- The open-<name>-vscode.cmd launcher body. Uses %~dp0 so it is independent
#     of the repo folder name; only the file NAME is set to match. -------------
$LauncherCmd = @'
@echo off
setlocal

set "AGENT_KAIZEN_ROOT=%~dp0"
cd /d "%AGENT_KAIZEN_ROOT%"

code "%AGENT_KAIZEN_ROOT%_workspace\agent-kaizen-tools.code-workspace"

endlocal
'@


function Write-Step {
    param([string] $Message)
    $script:Step++
    Write-Host ''
    Write-Host ("[{0}/{1}] {2}" -f $script:Step, $script:TotalSteps, $Message) -ForegroundColor Cyan
}

function Write-Info { param([string] $m) Write-Host "      $m" }
function Write-Ok   { param([string] $m) Write-Host "      [ok] $m" -ForegroundColor Green }
function Write-Warn { param([string] $m) Write-Host "      [warn] $m" -ForegroundColor Yellow }

function Write-Utf8NoBom {
    param([string] $Path, [string] $Content)
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $enc)
}

function Test-CommandExists {
    param([string] $Name)
    $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

# Rebuild the process PATH from the persisted Machine + User values. winget
# writes those during an install but does not refresh an already-running shell.
function Update-ProcessPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $merged  = @($machine, $user) | Where-Object { $_ }
    if ($merged) { $env:Path = ($merged -join ';') }
}

# Locate winget.exe even when it is only reachable via the WindowsApps alias.
function Find-Winget {
    $cmd = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @((Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\winget.exe'))
    $store = Join-Path $env:ProgramFiles 'WindowsApps'
    if (Test-Path $store) {
        $candidates += Get-ChildItem $store -Filter 'winget.exe' -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -ExpandProperty FullName
    }
    foreach ($p in $candidates) { if ($p -and (Test-Path $p)) { return $p } }
    return $null
}

# Return the path to a REAL python.exe (>=3.10), skipping the Microsoft Store
# App-Execution-Alias stub under WindowsApps, or $null if none is usable.
function Resolve-RealPython {
    $candidates = @()
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    $programs = Join-Path $env:LOCALAPPDATA 'Programs\Python'
    if (Test-Path $programs) {
        $candidates += Get-ChildItem $programs -Filter 'Python3*' -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName 'python.exe' }
    }
    foreach ($p in $candidates) {
        if (-not $p -or -not (Test-Path $p)) { continue }
        if ($p -like '*\WindowsApps\*') { continue }   # Store stub - not a real interpreter
        $v = & $p -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) {
            $parts = $v.Trim().Split('.')
            if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10)) { return $p }
        }
    }
    return $null
}

function Stop-NoWinget {
    param([string] $Missing)
    Write-Host ''
    Write-Host "Could not find winget to install the prerequisites ($Missing)." -ForegroundColor Red
    Write-Host 'Install these, ticking "Add to PATH", then run this installer again:'
    Write-Host '  Git for Windows : https://git-scm.com/download/win'
    Write-Host '  Python 3.12     : https://www.python.org/downloads/   (check "Add python.exe to PATH")'
    Write-Host 'Optional: install "App Installer" (winget) from the Microsoft Store, then re-run.'
    throw "Prerequisite '$Missing' is missing and winget is unavailable."
}


# ============================================================================
# Step 1 - Prerequisites (install via winget when missing)
# ============================================================================
function Install-Prereqs {
    Write-Step 'Ensuring prerequisites (git, Python)'
    $winget = Find-Winget

    # --- git ----------------------------------------------------------------
    if (Test-CommandExists 'git') {
        Write-Ok ('git: ' + ((& git --version) -join ''))
    } else {
        if (-not $winget) { Stop-NoWinget 'git' }
        Write-Info 'git not found; installing Git.Git via winget (user scope)...'
        & $winget install --id Git.Git -e --scope user --silent --accept-package-agreements --accept-source-agreements
        Update-ProcessPath
        $gitCmd = Join-Path $env:LOCALAPPDATA 'Programs\Git\cmd'
        if (Test-Path (Join-Path $gitCmd 'git.exe')) { $env:Path = "$gitCmd;$env:Path" }
        for ($i = 0; $i -lt 3 -and -not (Test-CommandExists 'git'); $i++) { Start-Sleep -Seconds 1; Update-ProcessPath }
        if (-not (Test-CommandExists 'git')) { throw 'git was installed but is still not callable. Open a new terminal and re-run.' }
        Write-Ok ('git installed: ' + ((& git --version) -join ''))
    }

    # --- Python -------------------------------------------------------------
    $py = Resolve-RealPython
    if (-not $py) {
        if (-not $winget) { Stop-NoWinget 'Python' }
        Write-Info 'Python 3.10+ not found; installing Python.Python.3.12 via winget (user scope)...'
        & $winget install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements
        Update-ProcessPath
        for ($i = 0; $i -lt 3 -and -not ($py = Resolve-RealPython); $i++) { Start-Sleep -Seconds 1; Update-ProcessPath }
        if (-not $py) { throw 'Python was installed but no usable interpreter was found. Open a new terminal and re-run.' }
    }
    # Put the real Python ahead of the WindowsApps stub for this process.
    $pyDir = Split-Path $py -Parent
    $env:Path = "$pyDir;$pyDir\Scripts;$env:Path"
    $script:SystemPython = $py
    $ver = (& $py -c "import sys;print('%d.%d' % sys.version_info[:2])").Trim()
    if ($ver -ne '3.12') { Write-Warn "Python $ver detected; this repo is developed on 3.12." }
    Write-Ok "python: $ver  ($py)"
}


# ============================================================================
# Step 2 - DEVROOT (choose + create; persisted after the clone)
# ============================================================================
function Resolve-DevRoot {
    Write-Step 'Setting DEVROOT'

    if (-not $DevRoot) {
        $default = if ($env:DEVROOT) { $env:DEVROOT } else { (Join-Path $env:SystemDrive 'dev') }
        if ($NoPrompt) {
            $script:DevRoot = $default
        } else {
            $answer = Read-Host "Parent dev folder (DEVROOT) [$default]"
            if ([string]::IsNullOrWhiteSpace($answer)) { $script:DevRoot = $default } else { $script:DevRoot = $answer }
        }
    } else {
        $script:DevRoot = $DevRoot
    }
    $script:DevRoot = [System.IO.Path]::GetFullPath($script:DevRoot)

    if ($script:DevRoot.StartsWith('\\')) { Write-Warn 'DEVROOT is a UNC path; a local drive (e.g. C:\dev) is recommended.' }
    if ($script:DevRoot.Length -gt 40)    { Write-Warn 'DEVROOT is a deep path; a short root (e.g. C:\dev) avoids long-path issues.' }

    if (-not (Test-Path $script:DevRoot)) {
        New-Item -ItemType Directory -Path $script:DevRoot -Force | Out-Null
        Write-Info "Created $script:DevRoot"
    }
    $env:DEVROOT = $script:DevRoot
    Write-Ok "DEVROOT = $script:DevRoot"
}


# ============================================================================
# Step 3 - Clone (then persist DEVROOT via the cloned SetDevRoot.cmd)
# ============================================================================
function Invoke-Clone {
    Write-Step 'Cloning the repository'
    $source = if ($RepoSource) { $RepoSource } else { $RepoUrl }

    if (-not $RepoName) { throw 'RepoName is empty.' }
    if ($RepoName -notmatch '^[A-Za-z0-9._-]+$') {
        throw "Invalid repo folder name '$RepoName' (use letters, digits, '.', '_', '-')."
    }
    $script:RepoPath = Join-Path $script:DevRoot $RepoName

    if (Test-Path (Join-Path $script:RepoPath 'kaizen.py')) {
        Write-Info "A clone already exists at $script:RepoPath."
        if (Test-Path (Join-Path $script:RepoPath '.git')) {
            & git -C $script:RepoPath pull --ff-only
            if ($LASTEXITCODE -ne 0) { Write-Warn 'git pull did not fast-forward; leaving the existing checkout as-is.' }
        }
        Write-Ok "Repo at $script:RepoPath"
    } elseif (Test-Path $script:RepoPath) {
        if (Test-Path (Join-Path $script:RepoPath '.git')) {
            & git -C $script:RepoPath pull --ff-only
            if ($LASTEXITCODE -ne 0) { Write-Warn 'git pull did not fast-forward; leaving the existing checkout as-is.' }
            Write-Ok "Repo at $script:RepoPath"
        } else {
            throw "A non-git folder already occupies $script:RepoPath. Choose another name or remove it; the installer will not overwrite it."
        }
    } else {
        & git clone $source $script:RepoPath
        if ($LASTEXITCODE -ne 0) { throw "git clone failed from $source" }
        Write-Ok "Repo at $script:RepoPath"
    }

    # Persist DEVROOT now that the cloned SetDevRoot.cmd exists (HKCU-only; never PATH).
    if ($NoPersistDevRoot) {
        Write-Warn "DEVROOT set for this process only (not persisted)."
    } else {
        $setDevRoot = Join-Path $script:RepoPath 'setup\SetDevRoot.cmd'
        if (Test-Path $setDevRoot) {
            & cmd /c "`"$setDevRoot`" `"$script:DevRoot`" /nopause" | Out-Null
            if ($LASTEXITCODE -eq 1) { throw "SetDevRoot.cmd failed (exit 1)." }
            Write-Ok 'DEVROOT persisted to the user environment'
        } else {
            & setx DEVROOT "$script:DevRoot" | Out-Null
            Write-Ok 'DEVROOT persisted to the user environment'
        }
    }
}


# ============================================================================
# Step 4 - Shared venv
# ============================================================================
function New-SharedVenv {
    Write-Step 'Creating the shared Python venv'
    $script:VenvDir = Join-Path $script:DevRoot 'Python\venvs\kaizen'
    $script:VenvPython = Join-Path $script:VenvDir 'Scripts\python.exe'

    if (Test-Path $script:VenvPython) {
        Write-Ok "venv already present: $script:VenvDir"
    } else {
        New-Item -ItemType Directory -Path (Split-Path $script:VenvDir -Parent) -Force | Out-Null
        & $script:SystemPython -m venv $script:VenvDir
        if ($LASTEXITCODE -ne 0) { throw 'python -m venv failed.' }
        Write-Ok "Created venv: $script:VenvDir"
    }
    & $script:VenvPython -m pip install --quiet --upgrade pip | Out-Null
}


# ============================================================================
# Step 5 - Dependencies
# ============================================================================
function Install-Deps {
    Write-Step 'Installing Python dependencies'
    $req = Join-Path $script:RepoPath 'requirements-kaizen.txt'
    if (-not (Test-Path $req)) { throw "requirements file not found: $req" }
    & $script:VenvPython -m pip install --quiet -r $req
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }
    Write-Ok 'Dependencies installed.'
}


# ============================================================================
# Step 6 - VS Code workspace + launcher
# ============================================================================
function New-VsCodeArtifacts {
    Write-Step 'Generating the VS Code workspace and launcher'
    $wsDir = Join-Path $script:RepoPath '_workspace'
    New-Item -ItemType Directory -Path $wsDir -Force | Out-Null
    $wsFile = Join-Path $wsDir 'agent-kaizen-tools.code-workspace'
    Write-Utf8NoBom -Path $wsFile -Content $WorkspaceJson
    Write-Ok "workspace: _workspace\agent-kaizen-tools.code-workspace"

    $launcherName = "open-$RepoName-vscode.cmd"
    $launcherPath = Join-Path $script:RepoPath $launcherName
    Write-Utf8NoBom -Path $launcherPath -Content $LauncherCmd
    Write-Ok "launcher: $launcherName"
}


# ============================================================================
# Step 7 - Empty skills store
# ============================================================================
function New-SkillsStore {
    Write-Step 'Scaffolding the sibling skills store'
    $store = Join-Path $script:DevRoot 'SKILLS\skills'
    New-Item -ItemType Directory -Path $store -Force | Out-Null
    $readme = Join-Path $script:DevRoot 'SKILLS\README.md'
    if (-not (Test-Path $readme)) {
        Write-Utf8NoBom -Path $readme -Content "# SKILLS store`n`nEmpty by default. Use the repo's setup\link-skills.ps1 to clone skill repos here and link them into the repo's .agents/skills + .claude/skills.`n"
    }
    Write-Ok "skills store: $store (empty)"
}


# ============================================================================
# Step 8 - Local DB init
# ============================================================================
function Initialize-Db {
    Write-Step 'Initializing the local Kaizen DB'
    $kaizen = Join-Path $script:RepoPath 'kaizen.py'
    $out = & $script:VenvPython $kaizen K1 --json 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "kaizen.py K1 failed:`n$out" }
    Write-Ok 'Local DB initialized (empty policy DB - no guardrails seeded, by design).'
    $script:DbOut = $out
}


# ============================================================================
# Step 9 - Health check
# ============================================================================
function Invoke-HealthCheck {
    Write-Step 'Health check'
    $ok = $true

    $wsFile = Join-Path $script:RepoPath '_workspace\agent-kaizen-tools.code-workspace'
    if (Test-Path $wsFile) { Write-Ok 'workspace file present' } else { Write-Warn 'workspace file missing'; $ok = $false }

    $launcher = Join-Path $script:RepoPath ("open-$RepoName-vscode.cmd")
    if (Test-Path $launcher) { Write-Ok 'launcher present' } else { Write-Warn 'launcher missing'; $ok = $false }

    if (Test-Path (Join-Path $script:DevRoot 'SKILLS\skills')) { Write-Ok 'skills store present' } else { Write-Warn 'skills store missing'; $ok = $false }

    try {
        $status = $script:DbOut | ConvertFrom-Json
        if ($status.status -eq 'OK') { Write-Ok 'DB status OK' } else { Write-Warn "DB status: $($status.status)"; $ok = $false }
    } catch {
        Write-Warn 'Could not parse DB status JSON.'; $ok = $false
    }

    Write-Host ''
    if ($ok) {
        Write-Host 'Agent Kaizen is ready.' -ForegroundColor Green
    } else {
        Write-Host 'Setup finished with warnings - review the [warn] lines above.' -ForegroundColor Yellow
    }
    Write-Host ("  DEVROOT : {0}" -f $script:DevRoot)
    Write-Host ("  Repo    : {0}" -f $script:RepoPath)
    Write-Host ("  Venv    : {0}" -f $script:VenvDir)
    Write-Host ("  Open it : {0}" -f (Join-Path $script:RepoPath ("open-$RepoName-vscode.cmd")))
    Write-Host '  Skills  : optional - run setup\link-skills.ps1 to clone and link a skills store.'
    return $ok
}


# ============================================================================
# Main
# ============================================================================
try {
    Write-Host '=== Agent Kaizen installer ===' -ForegroundColor White
    Install-Prereqs
    Resolve-DevRoot
    Invoke-Clone
    New-SharedVenv
    Install-Deps
    New-VsCodeArtifacts
    New-SkillsStore
    Initialize-Db
    $healthy = Invoke-HealthCheck
    $exit = if ($healthy) { 0 } else { 1 }
} catch {
    Write-Host ''
    Write-Host ("FAILED at step {0}/{1}: {2}" -f $script:Step, $script:TotalSteps, $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Nothing was force-overwritten. Fix the issue above and re-run - the installer is safe to repeat.'
    $exit = 1
}

if (-not $NoPause) {
    Write-Host ''
    Read-Host 'Press Enter to close'
}
exit $exit
