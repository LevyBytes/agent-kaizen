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
REM    SetDevRoot.cmd [path] [/nopause]
REM    SetDevRoot.cmd /?            (show help and exit)
REM      (no path)  auto-detects DEVROOT as the parent folder of this repo and,
REM                 if DEVROOT is unset, asks you to confirm it.
REM      path       uses that folder instead and skips the confirm prompt, e.g.
REM                 SetDevRoot.cmd X:\your\devroot
REM      /nopause   runs non-interactively: no prompts, no final pause. For
REM                 automation/CI. Alias: -q
REM      help       show usage and exit. Aliases: /?  /help  -h  --help
REM
REM  BEHAVIOR
REM    - Auto-detects devroot from this script's location (<repo>\setup -> <devroot>).
REM    - DEVROOT unset      -> confirm the detected value (or type another).
REM    - DEVROOT == target  -> reports and makes no change.
REM    - DEVROOT != target  -> asks before overwriting (interactive only).
REM    - Persists via setx (user scope), then RE-READS the registry to verify
REM      the stored value. Also exports DEVROOT to the calling session.
REM    - Prints a non-fatal check of expected siblings under the chosen devroot.
REM
REM  EXIT CODES
REM    0  DEVROOT set, or already correct.
REM    1  error (folder does not exist, or setx / verification failed).
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

REM --- Parse arguments: optional [path], /nopause (alias -q), and help --------
set "ARGPATH="
set "NOPAUSE="
:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="/?"       goto usage
if /i "%~1"=="/help"    goto usage
if /i "%~1"=="-h"       goto usage
if /i "%~1"=="--help"   goto usage
if /i "%~1"=="/nopause" ( set "NOPAUSE=1" & shift /1 & goto parse_args )
if /i "%~1"=="-q"       ( set "NOPAUSE=1" & shift /1 & goto parse_args )
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

REM --- Choose the target folder: an explicit path arg wins; else detected -----
if defined ARGPATH (
  set "TARGET=%ARGPATH%"
  echo Using path from argument: "%ARGPATH%"
) else (
  set "TARGET=%DETECTED%"
)

REM --- Read the currently persisted user-scope value, if any ------------------
REM  Read HKCU\Environment directly (the persisted value), not the merged
REM  process value. tokens=2,* -> %%A=type (REG_SZ), %%B=the value.
set "CURRENT="
for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v DEVROOT 2^>nul ^| find /i "DEVROOT"') do set "CURRENT=%%B"
if defined CURRENT (
  echo Current persisted DEVROOT: "%CURRENT%"
) else (
  echo Current persisted DEVROOT: ^(not set^)
)

REM --- If DEVROOT is unset and no explicit path: confirm detected (interactive)
REM  Read %ANS% only AFTER set /p, on separate lines, to dodge the parenthesised
REM  block early-expansion trap.
if defined CURRENT  goto have_target
if defined ARGPATH  goto have_target
if defined NOPAUSE  goto have_target
echo.
echo DEVROOT is not currently set.
set "ANS="
set /p "ANS=Use this detected DEVROOT? [Y/n], or type a different path: "
if not defined ANS    goto have_target
if /i "%ANS%"=="Y"    goto have_target
if /i "%ANS%"=="yes"  goto have_target
if /i "%ANS%"=="N"    goto user_declined
if /i "%ANS%"=="no"   goto user_declined
REM  Anything else is treated as a path to use instead.
for %%I in ("%ANS%") do set "TARGET=%%~fI"

:have_target
REM --- Validate the chosen target exists --------------------------------------
if not exist "%TARGET%\" (
  echo.
  echo ERROR: folder does not exist:  "%TARGET%"
  echo Nothing was changed. Pass an existing folder, e.g.  SetDevRoot.cmd X:\your\devroot
  set "FINAL_RC=1"
  set "FINAL_DEVROOT=%CURRENT%"
  goto end
)

REM --- Already correct? -------------------------------------------------------
if not defined CURRENT goto check_conflict
if /i "%CURRENT%"=="%TARGET%" (
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
echo.
echo DEVROOT is currently: "%CURRENT%"
echo New value would be:    "%TARGET%"
set "ANS="
set /p "ANS=Overwrite DEVROOT? [y/N] "
if /i not "%ANS%"=="Y" goto user_declined

:do_set
REM --- Persist via setx (user scope; never touches PATH) ----------------------
echo.
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

:usage
echo.
echo SetDevRoot.cmd  --  set the DEVROOT user environment variable
echo.
echo Usage:
echo   SetDevRoot.cmd [path] [/nopause]
echo   SetDevRoot.cmd /?
echo.
echo   (no path)  auto-detect DEVROOT as this repo's parent folder; if DEVROOT
echo              is unset, confirm it interactively.
echo   path       use that folder instead (skips the confirm prompt), e.g.
echo              SetDevRoot.cmd X:\your\devroot
echo   /nopause   non-interactive: no prompts, no final pause (alias -q).
echo   help        show this help. Aliases: /?  /help  -h  --help
echo.
echo Sets ONLY the DEVROOT user variable (via setx); never touches PATH. No admin
echo needed. Exit codes: 0 set/already-correct, 1 error, 2 declined.
endlocal & exit /b 0

:user_declined
echo.
echo Cancelled. DEVROOT left unchanged.
set "FINAL_RC=2"
set "FINAL_DEVROOT=%CURRENT%"
goto end

:end
REM --- Single exit point: summarise, pause (unless /nopause), export, exit -----
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
