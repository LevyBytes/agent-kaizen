#Requires -Version 5.1
<#
  Shared installer helpers for Agent Kaizen setup scripts.

  Keep this file Windows PowerShell 5.1-compatible. The one-file Windows
  bootstrapper embeds its own copy of the core runner so it can still be
  downloaded and executed alone; repo-local optional setup scripts dot-source
  this helper.
#>

$script:AkInstallerName = 'Agent Kaizen setup'
$script:AkRepoRoot = ''
$script:AkDevRoot = ''
$script:AkLogRoot = ''
$script:AkStatePath = ''
$script:AkSteps = @()
$script:AkStepIndex = @{}
$script:AkStepRecords = New-Object 'System.Collections.Generic.List[object]'
$script:AkCurrentStep = 0
$script:AkCurrentStepName = ''
$script:AkCommandCounter = 0
$script:AkPlanOnly = $false
$script:AkNoProgressHeader = $false
$script:AkNoNetwork = $false
$script:AkNoExternalActions = $false
$script:AkNoUserEnvWrites = $false
$script:AkAssumeYes = $false
$script:AkNoInput = $false
$script:AkSelfTest = $false

function Resolve-AkFullPath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return '' }
    return [System.IO.Path]::GetFullPath($Path)
}

function Resolve-AkDevRoot {
    param(
        [string]$DevRoot,
        [Parameter(Mandatory=$true)][string]$RepoRoot,
        [switch]$NoInput
    )
    if (-not [string]::IsNullOrWhiteSpace($DevRoot)) {
        return (Resolve-AkFullPath $DevRoot)
    }
    if (-not [string]::IsNullOrWhiteSpace($env:DEVROOT)) {
        return (Resolve-AkFullPath $env:DEVROOT)
    }
    $candidate = Resolve-AkFullPath (Split-Path -Parent $RepoRoot)
    $root = [System.IO.Path]::GetPathRoot($candidate)
    if ($root -and $root.TrimEnd('\') -ieq 'C:') {
        throw 'DEVROOT was not supplied and the repo parent is on the system drive. Pass -DevRoot explicitly.'
    }
    return $candidate
}

function New-AkDirectory {
    param([Parameter(Mandatory=$true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Write-AkFileUtf8NoBom {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$Text
    )
    $parent = Split-Path -Parent $Path
    if (-not [string]::IsNullOrWhiteSpace($parent)) { New-AkDirectory $parent }
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $utf8)
}

function Format-AkBytes {
    param([Nullable[Int64]]$Bytes)
    if ($null -eq $Bytes) { return 'unknown' }
    $b = [double]$Bytes
    if ($b -ge 1GB) { return ('{0:N2} GB' -f ($b / 1GB)) }
    if ($b -ge 1MB) { return ('{0:N1} MB' -f ($b / 1MB)) }
    if ($b -ge 1KB) { return ('{0:N0} KB' -f ($b / 1KB)) }
    return ('{0:N0} B' -f $b)
}

function Get-AkConsoleWidth {
    try {
        $w = $Host.UI.RawUI.WindowSize.Width
        if ($w -lt 60) { return 60 }
        return $w
    } catch {
        return 100
    }
}

function Fit-AkLine {
    param([string]$Text)
    $max = [Math]::Max(20, (Get-AkConsoleWidth) - 1)
    if ($null -eq $Text) { $Text = '' }
    if ($Text.Length -gt $max) { return $Text.Substring(0, $max) }
    return $Text.PadRight($max)
}

function Get-AkSafeTail {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [int]$MaxLines = 8
    )
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        try {
            $sr = New-Object System.IO.StreamReader($fs)
            try {
                $lines = New-Object 'System.Collections.Generic.List[string]'
                while (-not $sr.EndOfStream) { [void]$lines.Add($sr.ReadLine()) }
                $arr = @($lines.ToArray())
                if ($arr.Count -le $MaxLines) { return $arr }
                return @($arr[($arr.Count - $MaxLines)..($arr.Count - 1)])
            } finally { $sr.Dispose() }
        } finally { $fs.Dispose() }
    } catch {
        return @()
    }
}

function Initialize-AkInstaller {
    param(
        [Parameter(Mandatory=$true)][string]$Name,
        [Parameter(Mandatory=$true)][string]$RepoRoot,
        [Parameter(Mandatory=$true)][string]$DevRoot,
        [object[]]$Steps,
        [switch]$PlanOnly,
        [switch]$NoProgressHeader,
        [switch]$NoNetwork,
        [switch]$NoExternalActions,
        [switch]$NoUserEnvWrites,
        [switch]$AssumeYes,
        [switch]$NoInput,
        [switch]$SelfTest
    )
    $script:AkInstallerName = $Name
    $script:AkRepoRoot = Resolve-AkFullPath $RepoRoot
    $script:AkDevRoot = Resolve-AkFullPath $DevRoot
    $script:AkPlanOnly = [bool]$PlanOnly
    $script:AkNoProgressHeader = [bool]$NoProgressHeader
    $script:AkNoNetwork = [bool]$NoNetwork
    $script:AkNoExternalActions = [bool]$NoExternalActions
    $script:AkNoUserEnvWrites = [bool]$NoUserEnvWrites
    $script:AkAssumeYes = [bool]$AssumeYes
    $script:AkNoInput = [bool]$NoInput
    $script:AkSelfTest = [bool]$SelfTest
    $script:AkLogRoot = Join-Path $script:AkDevRoot 'agent-kaizen-setup\logs'
    $script:AkStatePath = Join-Path $script:AkDevRoot 'agent-kaizen-setup\setup-state.json'
    $script:AkSteps = @($Steps)
    $script:AkStepIndex = @{}
    for ($i = 0; $i -lt $script:AkSteps.Count; $i++) {
        $script:AkStepIndex[[string]$script:AkSteps[$i].Id] = ($i + 1)
    }
    if (-not ($script:AkPlanOnly -or $script:AkSelfTest)) {
        New-AkDirectory $script:AkLogRoot
    }
}

function Show-AkPlan {
    Write-Host ''
    Write-Host $script:AkInstallerName -ForegroundColor White
    Write-Host ('  RepoRoot: {0}' -f $script:AkRepoRoot)
    Write-Host ('  DEVROOT : {0}' -f $script:AkDevRoot)
    Write-Host ('  Logs    : {0}' -f $script:AkLogRoot)
    Write-Host ''
    Write-Host 'Planned setup steps:' -ForegroundColor Cyan
    $total = [Math]::Max(1, $script:AkSteps.Count)
    for ($i = 0; $i -lt $script:AkSteps.Count; $i++) {
        $from = [int][Math]::Floor(($i / [double]$total) * 100)
        $to = [int][Math]::Floor((($i + 1) / [double]$total) * 100)
        Write-Host ('{0,2}. {1,-58} {2,3}% -> {3,3}%' -f ($i + 1), [string]$script:AkSteps[$i].Name, $from, $to)
    }
}

function Get-AkPlanSnapshot {
    $items = New-Object 'System.Collections.Generic.List[object]'
    for ($i = 0; $i -lt $script:AkSteps.Count; $i++) {
        [void]$items.Add([ordered]@{
            index = $i + 1
            id = [string]$script:AkSteps[$i].Id
            name = [string]$script:AkSteps[$i].Name
        })
    }
    return [ordered]@{
        generatedAt = (Get-Date).ToString('s')
        installer = $script:AkInstallerName
        repoRoot = $script:AkRepoRoot
        devRoot = $script:AkDevRoot
        logRoot = $script:AkLogRoot
        planOnly = [bool]$script:AkPlanOnly
        selfTest = [bool]$script:AkSelfTest
        safety = [ordered]@{
            noNetwork = [bool]$script:AkNoNetwork
            noExternalActions = [bool]$script:AkNoExternalActions
            noUserEnvWrites = [bool]$script:AkNoUserEnvWrites
            noInput = [bool]$script:AkNoInput
        }
        steps = @($items.ToArray())
    }
}

function Write-AkPlanJson {
    param([Parameter(Mandatory=$true)][string]$Path)
    $json = [string]((Get-AkPlanSnapshot) | ConvertTo-Json -Depth 8)
    Write-AkFileUtf8NoBom -Path $Path -Text $json
    Write-Host ("Plan JSON written: {0}" -f $Path) -ForegroundColor Green
}

function Save-AkState {
    if ($script:AkPlanOnly -or $script:AkSelfTest) { return }
    try {
        $state = [ordered]@{
            installer = $script:AkInstallerName
            lastRun = (Get-Date).ToString('s')
            repoRoot = $script:AkRepoRoot
            devRoot = $script:AkDevRoot
            logRoot = $script:AkLogRoot
            steps = @($script:AkStepRecords.ToArray())
        }
        $json = [string]($state | ConvertTo-Json -Depth 8)
        Write-AkFileUtf8NoBom -Path $script:AkStatePath -Text $json
    } catch {
        Write-Warning ("Setup-state save skipped: {0}" -f $_.Exception.Message)
    }
}

function Write-AkProgress {
    param(
        [int]$Completed,
        [int]$StepNumber,
        [string]$StepName,
        [string]$Status
    )
    if ($script:AkNoProgressHeader) { return }
    $total = [Math]::Max(1, $script:AkSteps.Count)
    $pct = [int][Math]::Floor(([Math]::Max(0, [Math]::Min($total, $Completed)) / [double]$total) * 100)
    Write-Host ('[{0,3}%] [{1}/{2}] {3}: {4}' -f $pct, $StepNumber, $total, $Status, $StepName) -ForegroundColor Cyan
}

function Invoke-AkStep {
    param(
        [Parameter(Mandatory=$true)][string]$Id,
        [Parameter(Mandatory=$true)][string]$Name,
        [Parameter(Mandatory=$true)][scriptblock]$ScriptBlock
    )
    $stepNo = if ($script:AkStepIndex.ContainsKey($Id)) { [int]$script:AkStepIndex[$Id] } else { $script:AkCurrentStep + 1 }
    $script:AkCurrentStep = $stepNo
    $script:AkCurrentStepName = $Name
    $before = [Math]::Max(0, $stepNo - 1)
    $start = Get-Date
    Write-AkProgress -Completed $before -StepNumber $stepNo -StepName $Name -Status 'working'
    Write-Host ''
    Write-Host ('=== [{0}/{1}] {2} ===' -f $stepNo, [Math]::Max(1, $script:AkSteps.Count), $Name) -ForegroundColor White
    try {
        if ($script:AkPlanOnly) {
            Write-Host 'Plan-only: step not executed.' -ForegroundColor DarkGray
            $status = 'PLAN'
            $reason = ''
        } elseif ($script:AkSelfTest) {
            Write-Host 'Self-test: validating step shape only; no external actions.' -ForegroundColor DarkGray
            $status = 'SELFTEST'
            $reason = ''
        } else {
            & $ScriptBlock
            $status = 'OK'
            $reason = ''
        }
    } catch {
        $status = 'FAILED'
        $reason = $_.Exception.Message
        Write-Host ("FAILED: {0}" -f $reason) -ForegroundColor Red
        throw
    } finally {
        $elapsed = (Get-Date) - $start
        [void]$script:AkStepRecords.Add([ordered]@{
            id = $Id
            name = $Name
            status = $status
            elapsedSeconds = [Math]::Round($elapsed.TotalSeconds, 1)
            reason = $reason
        })
        Save-AkState
        $done = if ($status -eq 'FAILED') { $before } else { $stepNo }
        Write-AkProgress -Completed $done -StepNumber $stepNo -StepName $Name -Status $status
    }
}

function Assert-AkExternalAllowed {
    param([string]$Display)
    if ($script:AkNoExternalActions -or $script:AkPlanOnly -or $script:AkSelfTest) {
        throw "External command blocked by installer safety mode: $Display"
    }
}

function Assert-AkNetworkAllowed {
    param([string]$Uri)
    if ($script:AkNoNetwork -or $script:AkPlanOnly -or $script:AkSelfTest) {
        throw "Network request blocked by installer safety mode: $Uri"
    }
}

function Get-AkCommandLogPath {
    param([Parameter(Mandatory=$true)][string]$Exe)
    New-AkDirectory $script:AkLogRoot
    $script:AkCommandCounter += 1
    $leaf = [System.IO.Path]::GetFileNameWithoutExtension($Exe)
    if ([string]::IsNullOrWhiteSpace($leaf)) { $leaf = 'command' }
    $leaf = ($leaf -replace '[^A-Za-z0-9_.-]', '_')
    $name = 'command-{0}-{1:000}-{2}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $script:AkCommandCounter, $leaf
    return (Join-Path $script:AkLogRoot $name)
}

function Write-AkCommandDashboard {
    param(
        [string]$Display,
        [string]$LogPath,
        [TimeSpan]$Elapsed,
        [string]$Spinner,
        [string]$Status,
        [string]$Note
    )
    if ($script:AkNoProgressHeader) { return }
    try { Clear-Host } catch { }
    $completed = [Math]::Max(0, $script:AkCurrentStep - 1)
    $total = [Math]::Max(1, $script:AkSteps.Count)
    $pct = [int][Math]::Floor(($completed / [double]$total) * 100)
    $elapsedText = '{0:00}:{1:00}:{2:00}' -f [int]$Elapsed.TotalHours, $Elapsed.Minutes, $Elapsed.Seconds
    Write-Host (Fit-AkLine ('Agent Kaizen setup  {0}%  step {1}/{2}  {3}' -f $pct, $script:AkCurrentStep, $total, $Status)) -ForegroundColor Cyan
    Write-Host (Fit-AkLine ('ACTIVE {0} elapsed {1}: {2}' -f $Spinner, $elapsedText, $Display)) -ForegroundColor Yellow
    Write-Host (Fit-AkLine ('LOG: {0}' -f $LogPath)) -ForegroundColor DarkGray
    if (-not [string]::IsNullOrWhiteSpace($Note)) { Write-Host (Fit-AkLine ('NOTE: {0}' -f $Note)) -ForegroundColor DarkYellow }
    if (Test-Path -LiteralPath $LogPath) {
        $item = Get-Item -LiteralPath $LogPath -ErrorAction SilentlyContinue
        if ($item) { Write-Host (Fit-AkLine ('WATCH: command log size {0}, last write {1}' -f (Format-AkBytes $item.Length), $item.LastWriteTime.ToString('HH:mm:ss'))) -ForegroundColor DarkYellow }
    }
    Write-Host ''
    Write-Host 'Recent command output:' -ForegroundColor DarkCyan
    foreach ($line in (Get-AkSafeTail -Path $LogPath -MaxLines 8)) {
        if (-not [string]::IsNullOrWhiteSpace($line)) { Write-Host (Fit-AkLine ('  ' + $line)) -ForegroundColor DarkGray }
    }
}

function Invoke-AkNative {
    param(
        [Parameter(Mandatory=$true)][string]$Exe,
        [string[]]$Arguments = @(),
        [string]$ActivityNote = '',
        [string]$WorkingDirectory = '',
        [switch]$IgnoreExitCode
    )
    $display = $Exe
    if ($Arguments.Count -gt 0) { $display = "$Exe $($Arguments -join ' ')" }
    Assert-AkExternalAllowed $display

    $log = Get-AkCommandLogPath $Exe
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($log, "COMMAND: $display`r`nSTARTED: $((Get-Date).ToString('s'))`r`n", $utf8)
    if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) {
        Add-Content -LiteralPath $log -Value ("WORKDIR: {0}" -f $WorkingDirectory) -Encoding UTF8
    }
    $exitFile = "$log.exit"
    Remove-Item -LiteralPath $exitFile -Force -ErrorAction SilentlyContinue

    $job = Start-Job -ScriptBlock {
        param([string]$JobExe, [string[]]$JobArguments, [string]$JobLog, [string]$JobExitFile, [string]$JobWorkingDirectory)
        $ErrorActionPreference = 'Continue'
        try {
            if (-not [string]::IsNullOrWhiteSpace($JobWorkingDirectory)) { Set-Location -LiteralPath $JobWorkingDirectory }
            & $JobExe @JobArguments 2>&1 | ForEach-Object {
                try { Add-Content -LiteralPath $JobLog -Value ($_.ToString()) -Encoding UTF8 } catch {}
            }
            $ec = $LASTEXITCODE
            if ($null -eq $ec) { $ec = 0 }
        } catch {
            try { Add-Content -LiteralPath $JobLog -Value ('ERROR: ' + $_.Exception.Message) -Encoding UTF8 } catch {}
            $ec = 1
        }
        try { Add-Content -LiteralPath $JobLog -Value "`r`nFINISHED: $((Get-Date).ToString('s'))`r`nEXIT CODE: $ec" -Encoding UTF8 } catch {}
        try { Set-Content -LiteralPath $JobExitFile -Value ([string]$ec) -Encoding ASCII } catch {}
    } -ArgumentList $Exe, ([string[]]$Arguments), $log, $exitFile, $WorkingDirectory

    $frames = @('|','/','-','\')
    $tick = 0
    $start = Get-Date
    while ((Get-Job -Id $job.Id).State -eq 'Running') {
        $elapsed = (Get-Date) - $start
        if ($elapsed.TotalMilliseconds -ge 700) {
            Write-AkCommandDashboard -Display $display -LogPath $log -Elapsed $elapsed -Spinner $frames[$tick % $frames.Count] -Status 'RUNNING' -Note $ActivityNote
            Start-Sleep -Seconds 1
            $tick += 1
        } else {
            Start-Sleep -Milliseconds 100
        }
    }
    Wait-Job -Job $job | Out-Null
    Receive-Job -Job $job -ErrorAction SilentlyContinue | Out-Null
    Remove-Job -Job $job -Force -ErrorAction SilentlyContinue

    $exitCode = 1
    if (Test-Path -LiteralPath $exitFile) {
        try { $exitCode = [int]((Get-Content -LiteralPath $exitFile -ErrorAction Stop | Select-Object -First 1).Trim()) } catch { $exitCode = 1 }
        Remove-Item -LiteralPath $exitFile -Force -ErrorAction SilentlyContinue
    }
    $status = if ($exitCode -eq 0) { 'COMPLETE' } else { 'FAILED' }
    Write-AkCommandDashboard -Display $display -LogPath $log -Elapsed ((Get-Date) - $start) -Spinner '*' -Status $status -Note $ActivityNote
    if ($exitCode -ne 0) {
        Write-Host ("Command failed with exit code {0}. Log: {1}" -f $exitCode, $log) -ForegroundColor Red
        foreach ($line in (Get-AkSafeTail -Path $log -MaxLines 12)) { Write-Host ('  ' + $line) -ForegroundColor DarkGray }
        if (-not $IgnoreExitCode) { throw "Command failed with exit code ${exitCode}: $display" }
    }
    return $exitCode
}

function Invoke-AkDownload {
    param(
        [Parameter(Mandatory=$true)][string]$Uri,
        [Parameter(Mandatory=$true)][string]$OutFile,
        [switch]$Force
    )
    Assert-AkNetworkAllowed $Uri
    if ((Test-Path -LiteralPath $OutFile) -and (-not $Force)) {
        Write-Host ("Already downloaded: {0}" -f $OutFile) -ForegroundColor Green
        return
    }
    $parent = Split-Path -Parent $OutFile
    if (-not [string]::IsNullOrWhiteSpace($parent)) { New-AkDirectory $parent }
    $partial = "$OutFile.partial"
    if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue }
    $log = Get-AkCommandLogPath 'download'
    Add-Content -LiteralPath $log -Value ("DOWNLOAD: {0}" -f $Uri) -Encoding UTF8
    Add-Content -LiteralPath $log -Value ("OUTFILE: {0}" -f $OutFile) -Encoding UTF8

    $request = [System.Net.HttpWebRequest]::Create($Uri)
    $request.UserAgent = 'Agent-Kaizen-Installer'
    $response = $null
    $inStream = $null
    $outStream = $null
    try {
        $response = $request.GetResponse()
        $total = [int64]$response.ContentLength
        if ($total -lt 0) { $total = 0 }
        $inStream = $response.GetResponseStream()
        $outStream = [System.IO.File]::Open($partial, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $buffer = New-Object byte[] 1048576
        [int64]$readTotal = 0
        $start = Get-Date
        $lastPaint = Get-Date
        while (($n = $inStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $outStream.Write($buffer, 0, $n)
            $readTotal += $n
            $now = Get-Date
            if (($now - $lastPaint).TotalMilliseconds -ge 500) {
                $elapsed = $now - $start
                $rate = if ($elapsed.TotalSeconds -gt 0) { [int64]($readTotal / $elapsed.TotalSeconds) } else { [int64]0 }
                if ($total -gt 0) {
                    $remaining = $total - $readTotal
                    $pct = [int][Math]::Floor(($readTotal / [double]$total) * 100)
                    $eta = if ($rate -gt 0) { [TimeSpan]::FromSeconds($remaining / [double]$rate) } else { [TimeSpan]::Zero }
                    $note = ('Downloaded {0} / {1} ({2}%), {3}/s, ETA {4}' -f (Format-AkBytes $readTotal), (Format-AkBytes $total), $pct, (Format-AkBytes $rate), $eta.ToString('hh\:mm\:ss'))
                } else {
                    $note = ('Downloaded {0}, {1}/s, total size unknown' -f (Format-AkBytes $readTotal), (Format-AkBytes $rate))
                }
                Add-Content -LiteralPath $log -Value $note -Encoding UTF8
                Write-AkCommandDashboard -Display ("Download $Uri") -LogPath $log -Elapsed $elapsed -Spinner '>' -Status 'RUNNING' -Note $note
                $lastPaint = $now
            }
        }
    } finally {
        if ($null -ne $outStream) { $outStream.Dispose() }
        if ($null -ne $inStream) { $inStream.Dispose() }
        if ($null -ne $response) { $response.Dispose() }
    }
    Move-Item -LiteralPath $partial -Destination $OutFile -Force
    Add-Content -LiteralPath $log -Value ("FINISHED: {0}" -f (Get-Date).ToString('s')) -Encoding UTF8
    Write-AkCommandDashboard -Display ("Download $Uri") -LogPath $log -Elapsed ([TimeSpan]::Zero) -Spinner '*' -Status 'COMPLETE' -Note ('Saved {0}' -f $OutFile)
}

function Resolve-AkPythonExe {
    param(
        [string]$PythonExe,
        [Parameter(Mandatory=$true)][string]$DevRoot,
        [switch]$RequireShared
    )
    if (-not [string]::IsNullOrWhiteSpace($PythonExe)) {
        if (Test-Path -LiteralPath $PythonExe) { return (Resolve-AkFullPath $PythonExe) }
        return $PythonExe
    }
    $winShared = Join-Path $DevRoot 'Python\venvs\kaizen\Scripts\python.exe'
    if (Test-Path -LiteralPath $winShared) { return $winShared }
    if ($RequireShared) { throw "Shared Agent Kaizen venv Python not found: $winShared. Run the core installer first or pass -PythonExe." }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw 'python not found on PATH and shared Agent Kaizen venv is missing.'
}
