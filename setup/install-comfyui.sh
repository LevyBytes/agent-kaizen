#!/usr/bin/env bash
# Guided install of ComfyUI as a DEVROOT sibling for Agent Kaizen's Y* backend.
set -euo pipefail

GPU=0
CUDA_INDEX="https://download.pytorch.org/whl/cu121"
REPO_URL="https://github.com/comfyanonymous/ComfyUI.git"
DEVROOT_ARG=""
PYTHON_EXE=""
LIST_STEPS=0
EMIT_PLAN_JSON=""

while [ $# -gt 0 ]; do
  case "$1" in
    --gpu) GPU=1 ;;
    --devroot) DEVROOT_ARG="${2:-}"; shift ;;
    --python-exe) PYTHON_EXE="${2:-}"; shift ;;
    --cuda-index) CUDA_INDEX="${2:-}"; shift ;;
    --repo) REPO_URL="${2:-}"; shift ;;
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
# installer-common.sh defaults preserve the safety selectors parsed above.
# shellcheck source=setup/installer-common.sh
source "$SCRIPT_DIR/installer-common.sh"

DEVROOT_RESOLVED="$(ak_resolve_devroot "$DEVROOT_ARG" "$REPO_ROOT")"
ak_init "Agent Kaizen ComfyUI installer" "$REPO_ROOT" "$DEVROOT_RESOLVED" \
  "$(ak_step_obj preflight 'Resolve DEVROOT, Python, Git, and target paths')" \
  "$(ak_step_obj clone 'Clone or validate ComfyUI repository')" \
  "$(ak_step_obj venv 'Create ComfyUI virtual environment')" \
  "$(ak_step_obj torch 'Install CPU or CUDA torch packages')" \
  "$(ak_step_obj deps 'Install ComfyUI requirements')" \
  "$(ak_step_obj summary 'Print start and verification commands')"

if [ "$LIST_STEPS" -eq 1 ] || [ "$AK_PLAN_ONLY" -eq 1 ]; then
  ak_show_plan
  [ -z "$EMIT_PLAN_JSON" ] || ak_write_plan_json "$EMIT_PLAN_JSON"
  exit 0
fi
[ "$AK_SELF_TEST" -eq 0 ] || ak_show_plan

TARGET="$DEVROOT_RESOLVED/ComfyUI"
VENV="$TARGET/.venv"
VENV_PY="$VENV/bin/python"
PYTHON=""
GIT=""

step_preflight() {
  PYTHON="$(ak_resolve_python "$PYTHON_EXE" "$DEVROOT_RESOLVED")"
  [ -n "$PYTHON" ] || ak_die "python not found; run the core installer first or pass --python-exe"
  GIT="$(command -v git || true)"
  [ -n "$GIT" ] || ak_die "git not found on PATH"
  if [ "$GPU" -eq 0 ] && command -v nvidia-smi >/dev/null 2>&1; then
    printf '  ! NVIDIA GPU detected; re-run with --gpu for a CUDA torch wheel. Continuing with CPU torch.\n' >&2
  elif [ "$GPU" -eq 1 ] && ! command -v nvidia-smi >/dev/null 2>&1; then
    printf '  ! CUDA torch requested, but nvidia-smi was not found; verify the NVIDIA driver before using ComfyUI.\n' >&2
  fi
  printf 'DEVROOT: %s\n' "$DEVROOT_RESOLVED"
  printf 'ComfyUI target: %s\n' "$TARGET"
  printf 'Python: %s\n' "$PYTHON"
}

step_clone() {
  if [ -f "$TARGET/main.py" ]; then
    printf 'ComfyUI already present: %s\n' "$TARGET"
  else
    ak_assert_network_allowed "$REPO_URL"
    ak_run --note "Git is cloning ComfyUI repository data; network speed controls the duration." -- "$GIT" clone --depth 1 "$REPO_URL" "$TARGET"
  fi
}

step_venv() {
  if [ ! -x "$VENV_PY" ]; then
    ak_run --note "Creating the ComfyUI Python virtual environment." -- "$PYTHON" -m venv "$VENV"
  else
    printf 'ComfyUI venv already exists: %s\n' "$VENV"
  fi
  ak_assert_network_allowed "pip upgrade in ComfyUI venv"
  ak_run --note "Upgrading pip inside the ComfyUI venv." -- "$VENV_PY" -m pip install --upgrade pip
}

step_torch() {
  ak_assert_network_allowed "torch package installation"
  if [ "$GPU" -eq 1 ]; then
    ak_run --note "Installing CUDA torch packages from $CUDA_INDEX." -- "$VENV_PY" -m pip install torch torchvision torchaudio --index-url "$CUDA_INDEX"
  else
    ak_run --note "Installing CPU torch packages." -- "$VENV_PY" -m pip install torch torchvision torchaudio
  fi
}

step_deps() {
  [ -f "$TARGET/requirements.txt" ] || ak_die "ComfyUI requirements file not found: $TARGET/requirements.txt"
  ak_assert_network_allowed "ComfyUI requirements installation"
  ak_run --note "Installing ComfyUI requirements; pip output is logged and tailed while this runs." -- "$VENV_PY" -m pip install -r "$TARGET/requirements.txt"
}

step_summary() {
  printf '\nComfyUI installed.\n'
  printf '  Models:    place checkpoints under %s/models/checkpoints/\n' "$TARGET"
  printf '  Start:     "%s" "%s/main.py"\n' "$VENV_PY" "$TARGET"
  printf '  URL:       http://127.0.0.1:8188  (override with KAIZEN_COMFYUI_URL or Y5 --endpoint)\n'
  printf '  Verify:    python kaizen.py Y5 --json\n'
}

ak_run_step preflight 'Resolve DEVROOT, Python, Git, and target paths' step_preflight
ak_run_step clone 'Clone or validate ComfyUI repository' step_clone
ak_run_step venv 'Create ComfyUI virtual environment' step_venv
ak_run_step torch 'Install CPU or CUDA torch packages' step_torch
ak_run_step deps 'Install ComfyUI requirements' step_deps
ak_run_step summary 'Print start and verification commands' step_summary
