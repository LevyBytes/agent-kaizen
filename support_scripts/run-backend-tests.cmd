@echo off
rem Run ONLY the backend-model tests (embedder / reranker / PII / Qwen judge -- the plan's model
rem implementations), not the whole harness suite. Uses the project venv's Python. It runs a preflight,
rem auto-launches the monitor GUI up front, runs the tests (test_model_integration = the live models
rem first, test_purge_test last), holds all four backends resident ~30s so you can WATCH the load in the
rem monitor, then purges any real-DB test rows (K7). Runs in THIS console so you see the results.
rem Pass-through args are forwarded, e.g.:
rem   run-backend-tests.cmd --no-live   |   run-backend-tests.cmd --no-gui --no-hold   |   run-backend-tests.cmd -k judge
setlocal
set "PY=%~dp0..\..\Python\venvs\kaizen\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0run_backend_tests.py" %*
endlocal
