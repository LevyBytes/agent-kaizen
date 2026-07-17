@echo off
rem 1-click Agent Kaizen backend monitor: opens the native PySide6 window.
rem Launches fire-and-forget with pythonw (no console) via `start`, so NO terminal window lingers.
rem Prefers the shared venv derived from the repo parent, then falls back to pythonw.exe on PATH.
rem Pass-through args (e.g. --interval 2.0, --limit 8) are forwarded to the GUI.
setlocal
set "PYW=%~dp0..\..\Python\venvs\kaizen\Scripts\pythonw.exe"
if not exist "%PYW%" (
  where pythonw.exe >nul 2>nul
  if errorlevel 1 (
    >&2 echo ERROR: shared venv pythonw.exe is absent and pythonw.exe is not on PATH.
    exit /b 1
  )
  set "PYW=pythonw.exe"
)
start "Agent Kaizen backend monitor" "%PYW%" "%~dp0model_monitor_gui.py" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
