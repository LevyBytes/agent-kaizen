#requires -Version 5.1
#Requires -Version 5.1
<#
.SYNOPSIS
  Test the Agent Kaizen Linux installer in a throwaway WSL instance (the Linux analog of the
  Windows Sandbox rig). Each run imports a fresh Ubuntu, runs the installer as a normal sudo user
  exactly as a bare-machine user would, checks the result, captures logs, then discards the instance.

.DESCRIPTION
  This is a maintainer tool, not a shipped installer. It gives a reproducible "bare machine" every
  run so the installer's own bootstrap (git/Python/build-toolchain, Rust, pyturso build) is exercised
  end to end.

  ISOLATION: with -Source local it stages the CURRENT working tree (including uncommitted changes)
  into a throwaway git repo under AI/work/wsl/src, and the bootstrap clones THAT into the instance's
  own DEVROOT and hands off to the cloned copy. The installer only ever reads the Windows repo (never
  writes it), and kaizen.py runs against the instance-local clone -- never the real AI/db.

.PARAMETER Source
  local  (default) test the current working tree via a staged throwaway repo.
  github test the published installer (git clone from GitHub) -- exercises committed code only.

.PARAMETER Ref            Git ref for -Source github (default: main).
.PARAMETER Rootfs         Explicit path to an Ubuntu rootfs tarball to import (overrides provisioning).
.PARAMETER BaseRootfsUrl  Download+cache a bare Ubuntu cloud rootfs for a truly bare test.
.PARAMETER Snapshot       Provision the base by exporting the local Ubuntu distro (offline, reliable;
                          may not be perfectly bare). This is the default when no Rootfs/BaseRootfsUrl.
.PARAMETER SnapshotDistro Distro to export for -Snapshot (default: Ubuntu).
.PARAMETER KeepInstance   Do not unregister the instance afterward (for post-mortem inspection).
.PARAMETER User           Non-root sudo user to create and run as (default: aktest).
.PARAMETER InstanceName   Fixed throwaway distro name; existing ak-* names may be replaced, while other existing names are rejected.
.PARAMETER InstallerArgs  Extra flags passed through to install-agent-kaizen.sh.
.PARAMETER Interactive    Use a real TTY, skip automated acceptance, and retain the instance for interactive inspection.
.PARAMETER DryRun         Print what would run without importing/installing.

.EXAMPLE
  powershell -NoProfile -File tests/wsl-sandbox-test.ps1 -Source local
.EXAMPLE
  powershell -NoProfile -File tests/wsl-sandbox-test.ps1 -Source local -BaseRootfsUrl https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64-root.tar.xz
#>
[CmdletBinding()]
param(
  [ValidateSet('local', 'github')][string] $Source = 'local',
  [string] $Ref = 'main',
  [string] $Rootfs = '',
  [string] $BaseRootfsUrl = '',
  [switch] $Snapshot,
  [string] $SnapshotDistro = 'Ubuntu',
  [switch] $KeepInstance,
  [ValidatePattern('^[a-z_][a-z0-9_-]{0,31}$')][string] $User = 'aktest',
  [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')][string] $InstanceName = '',
  [string] $InstallerArgs = '',
  [switch] $Interactive,
  [switch] $DryRun
)

# 'Continue' (not 'Stop'): git/robocopy/wsl/curl all write progress+notices to stderr, and Windows
# PowerShell wraps native stderr as terminating NativeCommandError under 'Stop'. We check $LASTEXITCODE
# explicitly at every step that matters instead.
$ErrorActionPreference = 'Continue'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$WslRoot = Join-Path $RepoRoot 'AI\work\wsl'
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Instance = if ($InstanceName) { $InstanceName } else { "ak-test-$Stamp" }
$InstDir = Join-Path $WslRoot "inst\$Instance"
$RunDir = Join-Path $WslRoot "runs\$Stamp"
$StageRepo = Join-Path $WslRoot 'src'
$RootfsCache = Join-Path $WslRoot 'rootfs'

function Write-Section($t) { Write-Host ''; Write-Host "=== $t ===" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "FAIL: $msg" -ForegroundColor Red; exit 1 }

# Resolve drive-letter paths and map X:\path\to\repo to /mnt/x/path/to/repo; UNC paths are unsupported.
function ConvertTo-WslPath([string] $winPath) {
  $full = (Resolve-Path $winPath).Path
  if ($full -notmatch '^[A-Za-z]:[\\/]') { throw "WSL path conversion requires a drive-letter path: $full" }
  $drive = $full.Substring(0, 1).ToLower()
  $rest = $full.Substring(2) -replace '\\', '/'
  return "/mnt/$drive$rest"
}

# Run a command inside the instance; stream + tee to the run log. Returns the exit code.
function Invoke-InInstance([string] $bashCommand, [string] $asUser = 'root', [string] $logName = '') {
  # Pass the script base64-encoded and decode+run it inside bash. The only arg wsl.exe sees is a
  # single ASCII line (`echo <b64> | base64 -d | bash`), so this is immune both to PowerShell/wsl.exe
  # native-arg mangling (multi-line, quotes, parens) and to the PS5.1 stdin BOM/CRLF re-encoding that
  # a piped here-string suffers. Output is streamed live to the console via Out-Host (and Tee'd to a
  # log when given); Out-Host consumes the pipeline so the function returns ONLY the exit code.
  if ($DryRun) { Write-Host "DRY (as $asUser): $(($bashCommand -split "`n")[0]) ..." -ForegroundColor DarkGray; return 0 }
  $lf = $bashCommand -replace "`r`n", "`n"
  $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($lf))
  $inner = "echo $b64 | base64 -d | bash"
  if ($logName) {
    $log = Join-Path $RunDir $logName
    & wsl.exe -d $Instance -u $asUser -- bash -lc $inner 2>&1 | Tee-Object -FilePath $log | Out-Host
  } else {
    & wsl.exe -d $Instance -u $asUser -- bash -lc $inner 2>&1 | Out-Host
  }
  return $LASTEXITCODE
}

# Best-effort: let a kept instance open in VS Code Remote-WSL without a first-connect download (the
# sandbox's egress can block it). (1) install the WSL extension host-side -- a missing extension is a
# prime blank-window cause for `--remote`; (2) seed the matching VS Code Server into ~/.vscode-server.
# Wrapped so it never fails the harness -- on any miss, VS Code falls back to a normal first connect.
function Initialize-VsCodeServer([string] $instance, [string] $asUser) {
  try {
    if (-not (Get-Command code -ErrorAction SilentlyContinue)) {
      Write-Host "  [vscode] 'code' not on PATH; skipping server pre-provision." -ForegroundColor DarkGray
      return
    }
    & code --install-extension ms-vscode-remote.remote-wsl --force | Out-Null
    $ver = @(& code --version)
    $commit = if ($ver.Count -ge 2) { ([string]$ver[1]).Trim() } else { '' }
    if ($commit -notmatch '^[0-9a-f]{40}$') {
      Write-Host "  [vscode] WSL extension ensured; commit undetected, server installs on first connect." -ForegroundColor DarkGray
      return
    }
    $tarball = Join-Path $RootfsCache "vscode-server-$commit.tar.gz"
    if (-not (Test-Path $tarball)) {
      Write-Host "  [vscode] downloading VS Code Server (commit $commit)..." -ForegroundColor DarkGray
      try { Invoke-WebRequest -UseBasicParsing -Uri "https://update.code.visualstudio.com/commit:$commit/server-linux-x64/stable" -OutFile $tarball -ErrorAction Stop }
      catch { Write-Host "  [vscode] server download failed; first-connect fallback. $($_.Exception.Message)" -ForegroundColor DarkGray; return }
    }
    $tarWsl = ConvertTo-WslPath $tarball
    $seed = @"
set -e
d="`$HOME/.vscode-server/bin/$commit"
[ -x "`$d/node" ] && { echo 'vscode-server already present'; exit 0; }
mkdir -p "`$d"
tar -xzf '$tarWsl' --strip-components=1 -C "`$d"
echo 'vscode-server seeded'
"@
    Invoke-InInstance $seed $asUser 'vscode-server.log' | Out-Null
    Write-Host "  [vscode] server pre-provisioned (commit $commit)." -ForegroundColor DarkGray
  } catch {
    Write-Host "  [vscode] pre-provision skipped: $($_.Exception.Message)" -ForegroundColor DarkGray
  }
}

# --- Preflight ---------------------------------------------------------------
Write-Section "Preflight"
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { Fail 'wsl.exe not found. Install WSL first.' }
New-Item -ItemType Directory -Force -Path $WslRoot, $InstDir, $RunDir, $RootfsCache | Out-Null
Write-Host "Instance : $Instance"
Write-Host "Source   : $Source"
Write-Host "Run logs : $RunDir"

# --- Stage the working tree (local source) -----------------------------------
if ($Source -eq 'local') {
  Write-Section "Stage working tree -> throwaway repo"
  # Mirror the working tree (minus .git and the private/scratch AI subdirs) into a throwaway repo,
  # then commit it so the bootstrap's `git clone` picks up the CURRENT (uncommitted) code.
  New-Item -ItemType Directory -Force -Path $StageRepo | Out-Null
  $exclude = @("$RepoRoot\.git", "$RepoRoot\AI\db", "$RepoRoot\AI\work", "$RepoRoot\AI\vc")
  if (-not $DryRun) {
    robocopy $RepoRoot $StageRepo /MIR /XD $exclude /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "robocopy failed staging the working tree (code $LASTEXITCODE)." }
    Push-Location $StageRepo
    try {
      if (-not (Test-Path (Join-Path $StageRepo '.git'))) { git init -q *>$null }
      git -c core.autocrlf=false add -A *>$null
      git -c user.email=wsl@test -c user.name=wsl -c commit.gpgsign=false commit -q -m "wsl-test $Stamp" --allow-empty *>$null
      if ($LASTEXITCODE -ne 0) { Write-Host "  [warn] staging commit exit $LASTEXITCODE" -ForegroundColor Yellow }
    } finally { Pop-Location }
    Write-Host "Staged   : $StageRepo"
  }
  $StageRepoWsl = ConvertTo-WslPath $StageRepo
  $BootstrapWsl = ConvertTo-WslPath (Join-Path $RepoRoot 'setup\install-agent-kaizen.sh')
}

# --- Resolve a base rootfs ---------------------------------------------------
Write-Section "Resolve base rootfs"
$rootfsPath = ''
if ($Rootfs) {
  if (-not (Test-Path $Rootfs)) { Fail "Rootfs not found: $Rootfs" }
  $rootfsPath = (Resolve-Path $Rootfs).Path
  Write-Host "Using supplied rootfs: $rootfsPath"
} elseif ($BaseRootfsUrl) {
  $leaf = ((($BaseRootfsUrl -split '[?#]', 2)[0]).TrimEnd('/') -split '/')[-1]
  if ([string]::IsNullOrWhiteSpace($leaf) -or $leaf.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0) {
    Fail "BaseRootfsUrl must end with a valid rootfs filename."
  }
  $rootfsPath = Join-Path $RootfsCache $leaf
  if (Test-Path $rootfsPath) {
    Write-Host "Using cached rootfs: $rootfsPath"
  } else {
    Write-Host "Downloading bare rootfs: $BaseRootfsUrl"
    if (-not $DryRun) {
      try { Invoke-WebRequest -UseBasicParsing -Uri $BaseRootfsUrl -OutFile $rootfsPath -ErrorAction Stop }
      catch { Fail "rootfs download failed: $($_.Exception.Message)" }
    }
  }
} else {
  # Default: snapshot the local Ubuntu distro (reliable + offline). Pass -BaseRootfsUrl for a bare image.
  $rootfsPath = Join-Path $RootfsCache "$SnapshotDistro-snapshot.tar"
  if ((Test-Path $rootfsPath) -and -not $Snapshot) {
    Write-Host "Using cached snapshot: $rootfsPath"
  } else {
    Write-Host "Exporting $SnapshotDistro to a base snapshot (this can take a minute)..."
    if (-not $DryRun) { & wsl.exe --export $SnapshotDistro $rootfsPath; if ($LASTEXITCODE -ne 0) { Fail "wsl --export $SnapshotDistro failed. Pass -BaseRootfsUrl or -Rootfs instead." } }
  }
  Write-Host "NOTE: a distro snapshot may not be perfectly bare. For a faithful bare-machine test pass -BaseRootfsUrl." -ForegroundColor Yellow
}

# --- Import + provision -------------------------------------------------------
Write-Section "Import instance + provision sudo user"
if (-not $DryRun) {
  # A fixed -InstanceName can already exist (e.g. from an interrupted run); replace it for a clean
  # start. Only auto-remove our own throwaway (ak-*) names -- never an existing real distro.
  $originalOutputEncoding = [Console]::OutputEncoding
  try {
    [Console]::OutputEncoding = [Text.Encoding]::Unicode
    $existing = ((& wsl.exe --list --quiet) -replace "`0", "") -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
  } finally {
    [Console]::OutputEncoding = $originalOutputEncoding
  }
  if ($existing -contains $Instance) {
    if ($Instance -like 'ak-*') {
      Write-Host "Replacing existing throwaway instance '$Instance'..." -ForegroundColor DarkGray
      & wsl.exe --terminate $Instance 2>&1 | Out-Null
      $replaceTerminateExit = $LASTEXITCODE
      & wsl.exe --unregister $Instance 2>&1 | Out-Null
      $replaceUnregisterExit = $LASTEXITCODE
      if ($replaceUnregisterExit -ne 0) { Fail "wsl --unregister failed replacing '$Instance' (exit $replaceUnregisterExit)." }
      if ($replaceTerminateExit -ne 0) { Write-Warning "wsl --terminate returned exit $replaceTerminateExit while replacing '$Instance'; unregister succeeded." }
      Remove-Item -Recurse -Force $InstDir -ErrorAction SilentlyContinue
    } else {
      Fail "A WSL distro named '$Instance' already exists and is not a throwaway (ak-*). Pick a different -InstanceName."
    }
  }
  & wsl.exe --import $Instance $InstDir $rootfsPath --version 2
  if ($LASTEXITCODE -ne 0) { Fail "wsl --import failed." }
}
$provision = @"
set -e
id -u $User >/dev/null 2>&1 || useradd -m -s /bin/bash $User
getent group sudo >/dev/null 2>&1 && usermod -aG sudo $User || true
echo '$User ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/$User
chmod 440 /etc/sudoers.d/$User
# WSL2 network reliability for apt against the CDN-fronted Ubuntu mirror: no IPv6 route (force
# IPv4), fragmentation drops on the default MTU (lower it), and flaky CDN edges under parallel
# pipelined fetches (force serial single-connection downloads + longer timeout + retries).
DEV=`$(ip route show default 2>/dev/null | awk '/default/{print `$5; exit}')
[ -n "`$DEV" ] && ip link set dev "`$DEV" mtu 1400 2>/dev/null || true
mkdir -p /etc/apt/apt.conf.d
printf 'Acquire::ForceIPv4 "true";\nAcquire::Retries "5";\nAcquire::http::Timeout "30";\nAcquire::http::Pipeline-Depth "0";\nAcquire::Queue-Mode "access";\n' > /etc/apt/apt.conf.d/99agent-kaizen-wsl
# The staged source repo lives on /mnt (foreign ownership over the 9p mount); let git clone it
# without the "dubious ownership" refusal. Throwaway instance, so trusting all repos is fine.
printf '[safe]\n\tdirectory = *\n' > /etc/gitconfig
# Default this instance's login user (so `wsl -d <name>` and VS Code Remote-WSL open as $User, not root).
printf '[user]\ndefault=$User\n' > /etc/wsl.conf
"@
if ((Invoke-InInstance $provision 'root' 'provision.log') -ne 0) { Fail 'user provisioning failed.' }

# --- Run the installer --------------------------------------------------------
Write-Section "Run installer as '$User'"
# Interactive runs drop --assume-yes/--no-input so the dependency menu + prompts show.
$autoFlags = if ($Interactive) { '' } else { '--assume-yes --no-input' }
if ($Source -eq 'local') {
  $cmd = "bash '$BootstrapWsl' --devroot ""`$HOME/dev"" --repo-source '$StageRepoWsl' $autoFlags $InstallerArgs"
} else {
  $url = "https://raw.githubusercontent.com/LevyBytes/agent-kaizen/$Ref/setup/install-agent-kaizen.sh"
  $cmd = "curl -fsSL '$url' -o /tmp/ak-install.sh && bash /tmp/ak-install.sh --devroot ""`$HOME/dev"" --ref '$Ref' $autoFlags $InstallerArgs"
}
if ($Interactive) {
  if ($DryRun) {
    Write-Host "DRY: would run interactively: $cmd" -ForegroundColor DarkGray; $installRc = 0
  } else {
    # Run via a runner script over a REAL tty (no base64 pipe), so `read` prompts and `[ -t 0 ]` work
    # and output is live. Write LF/no-BOM so bash is happy.
    $runner = Join-Path $RunDir 'runner.sh'
    [IO.File]::WriteAllText($runner, ("#!/usr/bin/env bash`n$cmd`n" -replace "`r`n", "`n"), (New-Object System.Text.UTF8Encoding $false))
    Write-Host "Answer the dependency prompts below (Enter = No):" -ForegroundColor Cyan
    & wsl.exe -d $Instance -u $User -e bash -l (ConvertTo-WslPath $runner)
    $installRc = $LASTEXITCODE
  }
} else {
  $installRc = Invoke-InInstance $cmd $User 'install.log'
}
Write-Host ("installer exit code: {0}" -f $installRc) -ForegroundColor (@{ $true = 'Green'; $false = 'Red' }[[bool]($installRc -eq 0)])

# --- Acceptance checks (automated runs only; interactive runs are eyeballed by the user) ----------
$checks = @'
set +e
py="$HOME/dev/Python/venvs/kaizen/bin/python"
echo "-- turso import --"
"$py" -c 'import turso; print("turso OK")'; pt=$?
echo "-- kaizen K1 --"
"$py" "$HOME/dev/agent-kaizen/kaizen.py" K1 --json >/dev/null 2>&1; k1=$?
echo "-- persisted env (fresh login shell) --"
bash -lc 'command -v cargo >/dev/null 2>&1'; cargo=$?
[ "$cargo" -eq 0 ] && echo cargo-on-path=yes || echo cargo-on-path=no
echo "RESULT pyturso=$pt kaizen_k1=$k1 cargo=$cargo"
'@
$acceptOut = ''
if (-not $DryRun -and -not $Interactive) {
  Write-Section "Acceptance checks"
  Invoke-InInstance $checks $User 'accept.log' | Out-Null
  $acceptFile = Join-Path $RunDir 'accept.log'
  if (Test-Path $acceptFile) { $acceptOut = Get-Content -Raw $acceptFile }
}

# --- Capture in-instance logs -------------------------------------------------
if (-not $DryRun) {
  Write-Section "Capture in-instance setup logs"
  $runDirWsl = ConvertTo-WslPath $RunDir
  Invoke-InInstance "mkdir -p '$runDirWsl/instance-logs'; cp -r ""`$HOME/dev/agent-kaizen-setup/logs/.""  '$runDirWsl/instance-logs/' 2>/dev/null || true; cp ""`$HOME/dev/agent-kaizen-setup/setup-state.json"" '$runDirWsl/' 2>/dev/null || true" $User | Out-Null
}

# --- Teardown -----------------------------------------------------------------
$keep = $KeepInstance -or $Interactive
if (-not $keep -and -not $DryRun) {
  Write-Section "Teardown"
  & wsl.exe --terminate $Instance 2>&1 | Out-Null
  $terminateExit = $LASTEXITCODE
  & wsl.exe --unregister $Instance 2>&1 | Out-Null
  $unregisterExit = $LASTEXITCODE
  if ($unregisterExit -ne 0) { throw "wsl --unregister failed for '$Instance' (exit $unregisterExit); retained $InstDir" }
  if ($terminateExit -ne 0) { Write-Warning "wsl --terminate returned exit $terminateExit for '$Instance'; unregister succeeded." }
  Remove-Item -Recurse -Force $InstDir -ErrorAction SilentlyContinue
  Write-Host "Unregistered $Instance"
} elseif ($keep -and -not $DryRun) {
  # Restart so /etc/wsl.conf takes effect and the next session logs in as $User (not root).
  & wsl.exe --terminate $Instance 2>&1 | Out-Null
  # Discover the installed workspace file -- the project name may differ from 'agent-kaizen' once the
  # rename/duplication paths land, so glob rather than hardcode.
  $find = 'ls -1 $HOME/dev/*/_workspace/*.code-workspace 2>/dev/null | head -n1'
  $ws = (& wsl.exe -d $Instance -u $User -e bash -lc $find | Select-Object -Last 1)
  if ($ws) { $ws = ([string]$ws).Trim() }
  $venv = "/home/$User/dev/Python/venvs/kaizen/bin/python"
  $ready = $false
  if ($ws) {
    # Confirm the install actually landed so we never point VS Code at an empty instance.
    $probe = "test -f '$ws' && test -x '$venv' && echo AK_READY || echo AK_INCOMPLETE"
    $ready = ((& wsl.exe -d $Instance -u $User -e bash -lc $probe | Select-Object -Last 1) -match 'AK_READY')
  }
  # Seed the VS Code Server + WSL extension so opening is instant and never blank.
  Initialize-VsCodeServer $Instance $User
  Write-Host "Kept instance '$Instance'. To use it:" -ForegroundColor Yellow
  Write-Host "  Shell   : wsl -d $Instance" -ForegroundColor Yellow
  if ($ready) {
    # Open from the WINDOWS side via Remote-WSL: Code.exe runs natively, so it needs no in-WSL interop
    # -- an imported throwaway distro often lacks it, and the in-WSL `code` shim then fails with "Exec
    # format error". The WSL extension + server are pre-seeded above, so this loads the folders + venv.
    Write-Host "  VS Code : code --remote wsl+$Instance $ws" -ForegroundColor Yellow
    Write-Host "  Install : clone + venv + workspace PRESENT -- VS Code will show the folders and the Python venv." -ForegroundColor Green
  } else {
    Write-Host "  VS Code : (install incomplete -- no workspace/venv found; re-run the install before opening)" -ForegroundColor Red
    Write-Host "  Install : INCOMPLETE in this instance (venv/workspace missing)." -ForegroundColor Red
  }
  Write-Host "  Remove  : wsl --unregister $Instance" -ForegroundColor Yellow
}

# --- Summary ------------------------------------------------------------------
Write-Section "Summary"
if ($DryRun) { Write-Host 'DRY RUN complete.' -ForegroundColor Yellow; exit 0 }
if ($Interactive) {
  Write-Host "Logs: $RunDir"
  if ($installRc -eq 0) { Write-Host 'Interactive install finished (exit 0).' -ForegroundColor Green; exit 0 }
  else { Write-Host "Interactive install exited $installRc." -ForegroundColor Red; exit $installRc }
}
$pass = $true
if ($installRc -ne 0) { $pass = $false; Write-Host "installer exit $installRc" -ForegroundColor Red }
if ($acceptOut -match 'RESULT pyturso=0 kaizen_k1=0 cargo=0') {
  Write-Host "turso import + kaizen K1 + fresh-login cargo path: OK" -ForegroundColor Green
} else {
  $pass = $false; Write-Host "acceptance checks did not pass:" -ForegroundColor Red; Write-Host $acceptOut
}
Write-Host "Logs: $RunDir"
if ($pass) { Write-Host 'PASS' -ForegroundColor Green; exit 0 } else { Write-Host 'FAIL' -ForegroundColor Red; exit 1 }
