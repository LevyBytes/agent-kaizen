@echo off
rem 1-click Agent Kaizen backend monitor: opens the native PySide6 window.
rem Launches with pythonw (no console) and detaches via `start`, so NO terminal window lingers after
rem the window opens. Prefers the project venv's pythonw (D:\dev\Python\venvs\kaizen), falling back to
rem pythonw on PATH. Pass-through args (e.g. --interval 2.0, --limit 8) are forwarded to the GUI.
setlocal
set "PYW=%~dp0..\..\Python\venvs\kaizen\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"
start "Agent Kaizen backend monitor" "%PYW%" "%~dp0model_monitor_gui.py" %*
endlocal
