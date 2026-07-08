#!/usr/bin/env bash
# Linux/macOS counterpart of run-backend-tests.cmd: run ONLY the backend-model tests (embedder /
# reranker / PII / Qwen judge -- the plan's model implementations) with the project venv's Python, not
# the whole harness suite. It runs a preflight, auto-launches the monitor GUI up front, runs the tests
# (test_model_integration = the live models first, test_purge_test last), holds all four backends
# resident ~30s so you can WATCH the load in the monitor, then purges any real-DB test rows (K7).
# Runs in THIS terminal so you see the results. Args pass through, e.g.:
#   ./run-backend-tests.sh --no-live   |   ./run-backend-tests.sh --no-gui --no-hold   |   ./run-backend-tests.sh -k judge
#
# Platform notes vs Windows: the GUI needs PySide6 + a display (skipped automatically when $DISPLAY is
# unset, e.g. headless/SSH); per-process VRAM comes straight from nvidia-smi (no typeperf needed).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="$here/../../Python/venvs/kaizen/bin/python"
[ -x "$py" ] || py="python3"
exec "$py" "$here/run_backend_tests.py" "$@"
