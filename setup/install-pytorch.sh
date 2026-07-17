#!/usr/bin/env bash
# Install the OPT-IN PyTorch extra (sentence-transformers embeddings).
# Usage: install-pytorch.sh [--cpu] [--cuda-index URL] [--devroot PATH] [--python-exe PATH]
#        install-pytorch.sh --list-steps|--plan-only|--self-test [--emit-plan-json PATH]
# Safety selectors: --no-network --no-external-actions --no-user-env-writes --assume-yes --no-input --no-progress.
# --no-user-env-writes, --assume-yes, and --no-input are accepted for cross-installer parity; this script has no persistence or prompt path.
# CUDA is the default; cu121 is a conservative compatibility index and may be overridden for newer GPUs.
set -euo pipefail

CPU=0
CUDA_INDEX="https://download.pytorch.org/whl/cu121"
DEVROOT_ARG=""
PYTHON_EXE=""
LIST_STEPS=0
EMIT_PLAN_JSON=""

usage() {
  printf '%s\n' 'Usage: install-pytorch.sh [--cpu] [--cuda-index URL] [--devroot PATH] [--python-exe PATH]' '       install-pytorch.sh --list-steps|--plan-only|--self-test [--emit-plan-json PATH]' 'Safety: --no-network --no-external-actions --no-user-env-writes --assume-yes --no-input --no-progress'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --cpu) CPU=1 ;;
    --cuda-index) [ $# -ge 2 ] || { printf '%s\n' '--cuda-index requires a value' >&2; exit 2; }; CUDA_INDEX="$2"; shift 2; continue ;;
    --devroot) [ $# -ge 2 ] || { printf '%s\n' '--devroot requires a value' >&2; exit 2; }; DEVROOT_ARG="$2"; shift 2; continue ;;
    --python-exe) [ $# -ge 2 ] || { printf '%s\n' '--python-exe requires a value' >&2; exit 2; }; PYTHON_EXE="$2"; shift 2; continue ;;
    --list-steps) LIST_STEPS=1 ;;
    --emit-plan-json) [ $# -ge 2 ] || { printf '%s\n' '--emit-plan-json requires a value' >&2; exit 2; }; EMIT_PLAN_JSON="$2"; shift 2; continue ;;
    --plan-only) AK_PLAN_ONLY=1 ;;
    --self-test) AK_SELF_TEST=1 ;;
    --no-progress) AK_NO_PROGRESS=1 ;;
    --no-network) AK_NO_NETWORK=1 ;;
    --no-external-actions) AK_NO_EXTERNAL=1 ;;
    --no-user-env-writes) AK_NO_USER_ENV=1 ;;
    --assume-yes) AK_ASSUME_YES=1 ;;
    --no-input) AK_NO_INPUT=1 ;;
    -h|--help) usage; exit 0 ;;
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

if [ "$AK_PLAN_ONLY" -eq 1 ]; then
  ak_show_plan
  [ -z "$EMIT_PLAN_JSON" ] || ak_write_plan_json "$EMIT_PLAN_JSON"
  exit 0
fi
if [ "$AK_SELF_TEST" -ne 0 ]; then
  ak_show_plan
  [ -z "$EMIT_PLAN_JSON" ] || ak_write_plan_json "$EMIT_PLAN_JSON"
fi

CACHE="$DEVROOT_RESOLVED/models"
PYTHON=""

# Resolve the required shared interpreter (or explicit override), then create the process-scoped HF cache.
step_preflight() {
  if [ ! -d "$CACHE" ]; then ak_assert_external_allowed "create model cache $CACHE"; fi
  mkdir -p "$CACHE"
  export HF_HOME="$CACHE"
  if [ -n "$PYTHON_EXE" ]; then
    PYTHON="$(ak_resolve_python "$PYTHON_EXE" "$DEVROOT_RESOLVED")"
  else
    PYTHON="$DEVROOT_RESOLVED/Python/venvs/kaizen/bin/python"
    [ -x "$PYTHON" ] || ak_die "shared kaizen venv not found at $PYTHON; run the core installer first or pass --python-exe" "$AK_EX_UNAVAILABLE"
  fi
  printf 'Installing into: %s\n' "$PYTHON"
  printf 'HF weight cache: %s\n' "$CACHE"
}

# Install CUDA torch from --cuda-index by default, or CPU-only torch when --cpu is set.
step_torch() {
  ak_assert_network_allowed "torch package installation"
  if [ "$CPU" -eq 1 ]; then
    ak_run --note "Installing CPU-only torch (selected via --cpu); pip output is logged and tailed while this runs." -- "$PYTHON" -m pip install torch
  else
    ak_run --note "Installing CUDA torch (GPU-first default) from $CUDA_INDEX." -- "$PYTHON" -m pip install torch --index-url "$CUDA_INDEX"
  fi
}

# Install the pinned sentence-transformers extra after validating its requirements file.
step_extra() {
  [ -f "$REPO_ROOT/requirements-pytorch.txt" ] || ak_die "requirements file not found: $REPO_ROOT/requirements-pytorch.txt"
  ak_assert_network_allowed "PyTorch extra requirements installation"
  ak_run --note "Installing sentence-transformers and pinned PyTorch extra dependencies." -- "$PYTHON" -m pip install -r "$REPO_ROOT/requirements-pytorch.txt"
}

# Print copy-ready current-shell exports and the B1 verification command.
step_summary() {
  printf '\nPyTorch embedding backend installed. Recommended current-shell exports (HF_HOME already applied):\n'
  printf '  export HF_HOME="%s"\n' "$CACHE"
  printf '  export KAIZEN_EMBED_BACKEND=sentence-transformers\n'
  printf '  export KAIZEN_EMBED_MODEL=codefuse-ai/F2LLM-v2-1.7B\n'
  printf '  Verify: "%s" "%s/kaizen.py" B1 --json\n' "$PYTHON" "$REPO_ROOT"
}

ak_run_step preflight 'Resolve DEVROOT, Python, and model cache' step_preflight
ak_run_step torch 'Install or validate torch wheel' step_torch
ak_run_step extra 'Install Agent Kaizen PyTorch extra' step_extra
ak_run_step summary 'Print backend environment and verification command' step_summary
