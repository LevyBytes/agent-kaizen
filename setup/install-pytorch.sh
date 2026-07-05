#!/usr/bin/env bash
# Install the OPT-IN PyTorch extra (sentence-transformers embeddings).
set -euo pipefail

GPU=0
CUDA_INDEX="https://download.pytorch.org/whl/cu121"
DEVROOT_ARG=""
PYTHON_EXE=""
LIST_STEPS=0
EMIT_PLAN_JSON=""

while [ $# -gt 0 ]; do
  case "$1" in
    --gpu) GPU=1 ;;
    --cuda-index) CUDA_INDEX="${2:-}"; shift ;;
    --devroot) DEVROOT_ARG="${2:-}"; shift ;;
    --python-exe) PYTHON_EXE="${2:-}"; shift ;;
    --list-steps) LIST_STEPS=1 ;;
    --emit-plan-json) EMIT_PLAN_JSON="${2:-}"; shift ;;
    --plan-only) AK_PLAN_ONLY=1 ;;
    --self-test) AK_SELF_TEST=1 ;;
    --no-progress) AK_NO_PROGRESS=1 ;;
    --no-network) AK_NO_NETWORK=1 ;;
    --no-external-actions) AK_NO_EXTERNAL=1 ;;
    --no-user-env-writes) AK_NO_USER_ENV=1 ;;
    --assume-yes) AK_ASSUME_YES=1 ;;
    --no-input) AK_NO_INPUT=1 ;;
    *) printf 'unknown arg: %s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if [ "$LIST_STEPS" -eq 1 ]; then AK_PLAN_ONLY=1; fi
# shellcheck source=setup/installer-common.sh
source "$SCRIPT_DIR/installer-common.sh"

DEVROOT_RESOLVED="$(ak_resolve_devroot "$DEVROOT_ARG" "$REPO_ROOT")"
ak_init "Agent Kaizen PyTorch extra installer" "$REPO_ROOT" "$DEVROOT_RESOLVED" \
  "$(ak_step_obj preflight 'Resolve DEVROOT, Python, and model cache')" \
  "$(ak_step_obj torch 'Install or validate torch wheel')" \
  "$(ak_step_obj extra 'Install Agent Kaizen PyTorch extra')" \
  "$(ak_step_obj summary 'Print backend environment and verification command')"

if [ "$LIST_STEPS" -eq 1 ] || [ "$AK_PLAN_ONLY" -eq 1 ]; then
  ak_show_plan
  [ -z "$EMIT_PLAN_JSON" ] || ak_write_plan_json "$EMIT_PLAN_JSON"
  exit 0
fi
[ "$AK_SELF_TEST" -eq 0 ] || ak_show_plan

CACHE="$DEVROOT_RESOLVED/models"
PYTHON=""

step_preflight() {
  mkdir -p "$CACHE"
  export HF_HOME="$CACHE"
  PYTHON="$(ak_resolve_python "$PYTHON_EXE" "$DEVROOT_RESOLVED")"
  [ -n "$PYTHON" ] || ak_die "python not found; run the core installer first or pass --python-exe"
  printf 'Installing into: %s\n' "$PYTHON"
  printf 'HF weight cache: %s\n' "$CACHE"
}

step_torch() {
  if [ "$GPU" -eq 1 ]; then
    ak_run --note "Installing CUDA torch from $CUDA_INDEX." -- "$PYTHON" -m pip install torch --index-url "$CUDA_INDEX"
  else
    ak_run --note "Installing CPU torch; pip output is logged and tailed while this runs." -- "$PYTHON" -m pip install torch
  fi
}

step_extra() {
  ak_run --note "Installing sentence-transformers and pinned PyTorch extra dependencies." -- "$PYTHON" -m pip install -r "$REPO_ROOT/requirements-pytorch.txt"
}

step_summary() {
  printf '\nPyTorch embedding backend installed. Current-shell settings:\n'
  printf '  export HF_HOME="%s"\n' "$CACHE"
  printf '  export KAIZEN_EMBED_BACKEND=sentence-transformers\n'
  printf '  export KAIZEN_EMBED_MODEL=all-MiniLM-L6-v2\n'
  printf '  Verify: "%s" "%s/kaizen.py" B1 --json\n' "$PYTHON" "$REPO_ROOT"
}

ak_run_step preflight 'Resolve DEVROOT, Python, and model cache' step_preflight
ak_run_step torch 'Install or validate torch wheel' step_torch
ak_run_step extra 'Install Agent Kaizen PyTorch extra' step_extra
ak_run_step summary 'Print backend environment and verification command' step_summary
