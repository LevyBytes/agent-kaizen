@echo off
setlocal EnableExtensions
REM ============================================================================
REM  SetDevRoot.cmd  --  Set the DEVROOT user environment variable
REM ----------------------------------------------------------------------------
REM  PURPOSE
REM    DEVROOT names the parent folder that holds this repo (and, in a full
REM    setup, the sibling SKILLS store, the agent-kaizen repo, and the shared
REM    Python venv). Tools, docs, and the VS Code workspace read DEVROOT instead
REM    of hardcoding a drive path, so the tree works wherever you put it. Docs
REM    refer to the value as  %DEVROOT%  (cmd) /  $env:DEVROOT  (PowerShell) /
REM    $DEVROOT  (POSIX).
REM
REM  USAGE
REM    SetDevRoot.cmd [path] [/nopause] [/planonly] [/liststeps] [/no-user-env-writes] [/assumeyes]
REM    SetDevRoot.cmd /?            (show help and exit)
REM      (no path)  interactively choose DEVROOT from:
REM                   1) D:\dev  2) C:\dev  3) Custom path
REM      path       uses that folder instead and skips the confirm prompt, e.g.
REM                 SetDevRoot.cmd X:\your\devroot
REM      /nopause   runs non-interactively: no prompts, no final pause. For
REM                 automation/CI. Requires a path unless DEVROOT is already set.
REM                 Alias: -q
REM      /planonly  show what would be set, but do not write HKCU\Environment.
REM      /liststeps show the DEVROOT setup plan and exit without writes.
REM      /no-user-env-writes
REM                 update only this cmd session; skip persistent user env writes.
REM      /assumeyes accept D:\dev when D: exists, or explicit paths, without
REM                 prompting. It never silently chooses C:\dev.
REM      help       show usage and exit. Aliases: /?  /help  -h  --help
REM
REM  BEHAVIOR
REM    - Auto-detects devroot from this script's location for diagnostics.
REM    - DEVROOT unset      -> interactively choose D:\dev, C:\dev, or custom.
REM    - DEVROOT == target  -> reports and makes no change.
REM    - DEVROOT != target  -> asks before overwriting (interactive only).
REM    - Persists via setx (user scope), then RE-READS the registry to verify
REM      the stored value. Also exports DEVROOT to the calling session.
REM    - Prints a non-fatal check of expected siblings under the chosen devroot.
REM
REM  EXIT CODES
REM    0  DEVROOT set, or already correct.
REM    1  error (invalid DEVROOT, missing drive/root, or setx / verification failed).
REM    2  user declined (kept the existing value / cancelled).
REM
REM  SAFETY
REM    Writes ONLY the DEVROOT user variable (HKCU\Environment). It never reads
REM    or rewrites PATH, so it cannot truncate or corrupt PATH. Needs no admin
REM    rights and changes nothing else. Never destructive without confirmation.
REM ============================================================================

title Set DEVROOT
set "FINAL_RC="
set "FINAL_DEVROOT="
REM  Snapshot this script's own folder BEFORE arg parsing. A plain shift moves
REM  %0, so %~dp0 read after the parse loop would be wrong; capture it now.
set "SCRIPT_DIR=%~dp0"

REM --- Parse arguments: optional [path], flags, and help ----------------------
set "ARGPATH="
set "NOPAUSE="
set "PLANONLY="
set "LISTSTEPS="
set "NOUSERENV="
set "ASSUMEYES="
:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="/?"       goto usage
if /i "%~1"=="/help"    goto usage
if /i "%~1"=="-h"       goto usage
if /i "%~1"=="--help"   goto usage
if /i "%~1"=="help"     goto usage
if /i "%~1"=="/nopause" ( set "NOPAUSE=1" & shift /1 & goto parse_args )
if /i "%~1"=="-q"       ( set "NOPAUSE=1" & shift /1 & goto parse_args )
if /i "%~1"=="/planonly"  ( set "PLANONLY=1" & shift /1 & goto parse_args )
if /i "%~1"=="-planonly"  ( set "PLANONLY=1" & shift /1 & goto parse_args )
if /i "%~1"=="/liststeps" ( set "LISTSTEPS=1" & shift /1 & goto parse_args )
if /i "%~1"=="-liststeps" ( set "LISTSTEPS=1" & shift /1 & goto parse_args )
if /i "%~1"=="/no-user-env-writes" ( set "NOUSERENV=1" & shift /1 & goto parse_args )
if /i "%~1"=="-no-user-env-writes" ( set "NOUSERENV=1" & shift /1 & goto parse_args )
if /i "%~1"=="/assumeyes" ( set "ASSUMEYES=1" & shift /1 & goto parse_args )
if /i "%~1"=="-assumeyes" ( set "ASSUMEYES=1" & shift /1 & goto parse_args )
if not defined ARGPATH  ( for %%I in ("%~1") do set "ARGPATH=%%~fI" )
shift /1
goto parse_args
:args_done

echo.
echo === Set DEVROOT ============================================================

REM --- Auto-detect devroot = the grandparent of this setup folder -------------
REM  %~dp0 is  <devroot>\<repo>\setup\ ; its grandparent "..\.." (fully
REM  qualified by %%~fI) is <devroot>. Using %%~fI avoids a manual trailing-
REM  backslash strip, which would corrupt a drive-root devroot ("D:\" -> "D:").
for %%I in ("%SCRIPT_DIR%..\..") do set "DETECTED=%%~fI"
echo Detecting devroot from this script's location:
echo   setup folder : "%SCRIPT_DIR%"
echo   detected     : "%DETECTED%"

REM --- Read the currently persisted user-scope value, if any ------------------
REM  Read HKCU\Environment directly (the persisted value), not the merged
REM  process value. tokens=2,* -> %%A=type (REG_SZ), %%B=the value.
set "CURRENT="
for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v DEVROOT 2^>nul ^| find /i "DEVROOT"') do set "CURRENT=%%B"
if defined CURRENT (
  echo Current persisted DEVROOT: "%CURRENT%"
  for %%I in ("%CURRENT%") do set "CURRENT_N=%%~fI"
) else (
  echo Current persisted DEVROOT: ^(not set^)
)

if defined LISTSTEPS goto liststeps

REM --- Choose the target folder ------------------------------------------------
if defined ARGPATH (
  set "TARGET=%ARGPATH%"
  echo Using path from argument: "%ARGPATH%"
  goto have_target
)
if defined NOPAUSE (
  if defined CURRENT (
    set "TARGET=%CURRENT%"
    goto have_target
  )
  echo.
  echo ERROR: DEVROOT is required in non-interactive mode.
  echo Pass a path, e.g.  SetDevRoot.cmd D:\dev /nopause
  set "FINAL_RC=1"
  set "FINAL_DEVROOT=%CURRENT%"
  goto end
)
if defined ASSUMEYES (
  if exist "D:\" (
    set "TARGET=D:\dev"
    goto have_target
  )
  if defined CURRENT (
    set "TARGET=%CURRENT%"
    goto have_target
  )
  echo.
  echo ERROR: D:\ is not available and /assumeyes will not silently choose C:\dev.
  echo Pass a path explicitly, e.g.  SetDevRoot.cmd X:\your\devroot /assumeyes
  set "FINAL_RC=1"
  set "FINAL_DEVROOT=%CURRENT%"
  goto end
)

call :choose_target_menu
if errorlevel 1 goto user_declined

:have_target
call :validate_target
if errorlevel 1 goto end

REM --- Already correct? -------------------------------------------------------
if not defined CURRENT goto check_conflict
if /i "%CURRENT_N%"=="%TARGET%" (
  echo.
  echo DEVROOT is already set to "%CURRENT%".  No change needed.
  set "FINAL_RC=0"
  set "FINAL_DEVROOT=%CURRENT%"
  goto siblings
)

:check_conflict
REM --- A different existing value: confirm before overwriting (interactive) ---
if not defined CURRENT goto do_set
if defined NOPAUSE     goto do_set
if defined PLANONLY    goto do_set
if defined ASSUMEYES   goto do_set
echo.
echo DEVROOT is currently: "%CURRENT%"
echo New value would be:    "%TARGET%"
set "ANS="
set /p "ANS=Overwrite DEVROOT? [y/N] "
set "ANS=%ANS:"=%"
if /i not "%ANS%"=="Y" goto user_declined

:do_set
REM --- Persist via setx (user scope; never touches PATH) ----------------------
echo.
if defined PLANONLY (
  echo Plan-only: would set DEVROOT = "%TARGET%" but no user environment write was made.
  set "FINAL_RC=0"
  set "FINAL_DEVROOT=%TARGET%"
  goto siblings
)
if defined NOUSERENV (
  echo No user environment write requested. This cmd session will use DEVROOT = "%TARGET%"; HKCU\Environment is unchanged.
  set "FINAL_RC=0"
  set "FINAL_DEVROOT=%TARGET%"
  goto siblings
)
echo Setting DEVROOT = "%TARGET%"  ^(user scope, via setx^) ...
setx DEVROOT "%TARGET%" >nul
if errorlevel 1 (
  echo ERROR: setx failed; DEVROOT was not changed.
  set "FINAL_RC=1"
  set "FINAL_DEVROOT=%CURRENT%"
  goto end
)

REM --- Verify by re-reading the persisted value -------------------------------
set "VERIFY="
for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v DEVROOT 2^>nul ^| find /i "DEVROOT"') do set "VERIFY=%%B"
if /i not "%VERIFY%"=="%TARGET%" (
  echo ERROR: verification failed. Registry now reads "%VERIFY%".
  set "FINAL_RC=1"
  set "FINAL_DEVROOT=%VERIFY%"
  goto end
)
echo Verified: HKCU\Environment\DEVROOT = "%VERIFY%"
echo This persists for NEW terminals and apps; already-open ones need a restart.
echo.
echo To use it in THIS shell now ^(no restart^):
echo   cmd         set "DEVROOT=%TARGET%"
echo   PowerShell  $env:DEVROOT='%TARGET%'
set "FINAL_RC=0"
set "FINAL_DEVROOT=%TARGET%"

:siblings
REM --- Non-fatal diagnostic: which expected siblings exist under the devroot? -
echo.
echo Expected siblings under "%FINAL_DEVROOT%":
set "ANYSIB="
for %%S in ("SKILLS" "agent-kaizen" "Python\venvs\kaizen") do (
  if exist "%FINAL_DEVROOT%\%%~S\" ( echo   [found]   %%~S & set "ANYSIB=1" ) else ( echo   [missing] %%~S )
)
if not defined ANYSIB echo   WARNING: none of the expected siblings were found here; double-check this devroot.
goto end

:choose_target_menu
REM Sets a validated TARGET from D:\dev, C:\dev, or a custom path; Cancel returns 1.
echo.
echo Choose where Agent Kaizen should keep repos, tools, and setup logs:
if exist "D:\" (
  echo   1^) D:\dev  ^(recommended^)
) else (
  echo   1^) D:\dev  ^(D: drive not available^)
)
echo   2^) C:\dev
echo   3^) Custom path
echo   C^) Cancel
choice /C 123C /N /M "DEVROOT [1/2/3/C]: "
if errorlevel 4 exit /b 1
if errorlevel 3 goto choose_custom_target
if errorlevel 2 (
  set "TARGET=C:\dev"
  goto confirm_target_choice
)
if not exist "D:\" (
  echo.
  echo ERROR: D:\ is not available in this Windows session.
  echo Choose another location.
  goto choose_target_menu
)
set "TARGET=D:\dev"
goto confirm_target_choice

:choose_custom_target
echo.
set "TARGET="
set /p "TARGET=Custom DEVROOT path: "
set "TARGET=%TARGET:"=%"
if not defined TARGET (
  echo ERROR: custom DEVROOT cannot be blank.
  goto choose_target_menu
)

:confirm_target_choice
call :validate_target
if errorlevel 1 (
  echo Choose another location.
  goto choose_target_menu
)
echo.
echo Selected DEVROOT: "%TARGET%"
choice /C YN /N /M "Use this location? Y/N: "
if errorlevel 2 goto choose_target_menu
exit /b 0

:validate_target
REM Fully qualify TARGET; require a non-root drive path outside Windows and Program Files.
if not defined TARGET (
  call :target_invalid "DEVROOT cannot be blank."
  exit /b 1
)
set "TARGET=%TARGET:"=%"
for %%I in ("%TARGET%") do set "TARGET=%%~fI"
for %%I in ("%TARGET%") do set "TARGET_DRIVE=%%~dI"
if not defined TARGET_DRIVE (
  call :target_invalid "DEVROOT must include a drive, for example D:\dev."
  exit /b 1
)
if not exist "%TARGET_DRIVE%\" (
  call :target_invalid "DEVROOT drive/root does not exist: %TARGET_DRIVE%\"
  exit /b 1
)
if /i "%TARGET%"=="%TARGET_DRIVE%\" (
  call :target_invalid "DEVROOT must be a folder path, not only a drive root."
  exit /b 1
)
call :reject_if_under_env "WINDIR" "Windows"
if errorlevel 1 exit /b 1
call :reject_if_under_env "ProgramFiles" "Program Files"
if errorlevel 1 exit /b 1
call :reject_if_under_env "ProgramFiles(x86)" "Program Files"
if errorlevel 1 exit /b 1
exit /b 0

:target_invalid
echo.
echo ERROR: %~1
echo Nothing was changed. Choose D:\dev, C:\dev, or pass a custom folder path.
set "FINAL_RC=1"
set "FINAL_DEVROOT=%CURRENT%"
exit /b 1

:reject_if_under_env
REM Args: environment-variable name and display name. Reject TARGET inside that root.
setlocal
set "ENV_NAME=%~1"
set "DISPLAY_NAME=%~2"
call set "PROTECTED_ROOT=%%%ENV_NAME%%%"
if not defined PROTECTED_ROOT ( endlocal & exit /b 0 )
call :is_under "%TARGET%" "%PROTECTED_ROOT%"
if not errorlevel 1 (
  endlocal & call :target_invalid "DEVROOT must not be inside %DISPLAY_NAME%."
  exit /b 1
)
endlocal & exit /b 0

:is_under
REM Exit 0 when arg1 equals or descends from arg2 at a path-component boundary; else 1.
setlocal
set "AK_CHILD=%~f1"
set "AK_PARENT=%~f2"
if not defined AK_PARENT ( endlocal & exit /b 1 )
powershell.exe -NoProfile -NonInteractive -Command "$c=$env:AK_CHILD.TrimEnd('\');$p=$env:AK_PARENT.TrimEnd('\');if($c.Equals($p,[StringComparison]::OrdinalIgnoreCase) -or $c.StartsWith($p+'\',[StringComparison]::OrdinalIgnoreCase)){exit 0};exit 1"
set "AK_UNDER_RC=%ERRORLEVEL%"
endlocal & exit /b %AK_UNDER_RC%

:usage
echo.
echo SetDevRoot.cmd  --  set the DEVROOT user environment variable
echo.
echo Usage:
echo   SetDevRoot.cmd [path] [/nopause] [/planonly] [/liststeps] [/no-user-env-writes] [/assumeyes]
echo   SetDevRoot.cmd /?
echo.
echo   (no path)  choose DEVROOT interactively:
echo              1^) D:\dev  2^) C:\dev  3^) Custom path
echo   path       use that folder instead (skips the confirm prompt), e.g.
echo              SetDevRoot.cmd X:\your\devroot
echo   /nopause   non-interactive: no prompts, no final pause (alias -q).
echo              Requires a path unless DEVROOT is already set.
echo   /planonly  show what would be set; do not write HKCU\Environment.
echo   /liststeps show the setup plan; do not write HKCU\Environment.
echo   /no-user-env-writes  update this cmd session only; skip persistent writes.
echo   /assumeyes accept D:\dev when D: exists or explicit paths without prompting.
echo              It never silently chooses C:\dev.
echo   help        show this help. Aliases: /?  /help  -h  --help
echo.
echo Sets ONLY the DEVROOT user variable (via setx); never touches PATH. No admin
echo needed. Exit codes: 0 set/already-correct, 1 error, 2 declined.
endlocal & exit /b 0

:liststeps
REM /liststeps is static and exits before target selection; /planonly selects and validates.
echo.
echo Planned SetDevRoot steps:
echo   1. Choose DEVROOT from explicit path, current DEVROOT, D:\dev, or the menu.
echo   2. Read the currently persisted HKCU\Environment\DEVROOT value.
echo   3. Validate the drive exists and the target is not a drive root or system path.
echo   4. Persist DEVROOT unless /planonly or /no-user-env-writes is set.
echo   5. Report expected sibling folders under DEVROOT.
echo.
echo Plan only: no user environment writes were made.
endlocal & exit /b 0

:user_declined
echo.
echo Cancelled. DEVROOT left unchanged.
set "FINAL_RC=2"
set "FINAL_DEVROOT=%CURRENT%"
goto end

:end
REM --- Single exit point: summarise, pause (unless /nopause), export, exit -----
if not defined FINAL_RC set "FINAL_RC=1"
echo.
if "%FINAL_RC%"=="0" (
  echo Done.
) else (
  echo Finished with issues ^(exit %FINAL_RC%^).
)
if not defined NOPAUSE (
  echo.
  pause
)
REM  Export DEVROOT to the calling session too (survives endlocal). %FINAL_*%
REM  are expanded on this line BEFORE endlocal runs, so the values persist.
if defined FINAL_DEVROOT ( endlocal & set "DEVROOT=%FINAL_DEVROOT%" & exit /b %FINAL_RC% )
endlocal & exit /b %FINAL_RC%
