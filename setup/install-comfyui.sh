#!/usr/bin/env bash
# Guided install of ComfyUI as a $DEVROOT sibling, for Agent Kaizen's Y* (comfy-*) backend.
#
# Clones ComfyUI OUTSIDE the agent-kaizen repo (so multi-GB weights are never tracked by git),
# creates a venv, installs requirements (CPU default; --gpu for a CUDA wheel), and prints the
# start command. Model weights are NOT auto-downloaded. Run this yourself; Kaizen only needs the
# endpoint URL (default http://127.0.0.1:8188).
#
# Usage: bash setup/install-comfyui.sh [--gpu] [--devroot DIR] [--cuda-index URL]
set -euo pipefail

GPU=0
CUDA_INDEX="https://download.pytorch.org/whl/cu121"
REPO_URL="https://github.com/comfyanonymous/ComfyUI.git"
DEVROOT_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --gpu) GPU=1 ;;
    --devroot) DEVROOT_ARG="${2:-}"; shift ;;
    --cuda-index) CUDA_INDEX="${2:-}"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '==> %s\n' "$1"; }
die() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DEVROOT="${DEVROOT_ARG:-${DEVROOT:-$(dirname "$REPO_ROOT")}}"
TARGET="$DEVROOT/ComfyUI"
step "DEVROOT:        $DEVROOT"
step "ComfyUI target: $TARGET"

command -v git >/dev/null 2>&1 || die "git not found on PATH."
PYTHON="$(command -v python3 || command -v python || true)"
[ -n "$PYTHON" ] || die "python not found on PATH."

if [ "$GPU" -eq 0 ] && command -v nvidia-smi >/dev/null 2>&1; then
  echo "  ! NVIDIA GPU detected; re-run with --gpu for a CUDA torch wheel. Continuing with CPU torch." >&2
fi

if [ -d "$TARGET" ]; then
  step "ComfyUI already present (skipping clone)."
else
  step "Cloning ComfyUI..."
  git clone --depth 1 "$REPO_URL" "$TARGET"
fi

VENV="$TARGET/.venv"
VENV_PY="$VENV/bin/python"
if [ ! -x "$VENV_PY" ]; then
  step "Creating venv at $VENV ..."
  "$PYTHON" -m venv "$VENV"
fi
"$VENV_PY" -m pip install --upgrade pip >/dev/null

if [ "$GPU" -eq 1 ]; then
  step "Installing CUDA torch from $CUDA_INDEX ..."
  "$VENV_PY" -m pip install torch torchvision torchaudio --index-url "$CUDA_INDEX"
else
  step "Installing CPU torch ..."
  "$VENV_PY" -m pip install torch torchvision torchaudio
fi
step "Installing ComfyUI requirements ..."
"$VENV_PY" -m pip install -r "$TARGET/requirements.txt"

echo ""
step "ComfyUI installed."
echo "  Models:    place a checkpoint under $TARGET/models/checkpoints/  (not auto-downloaded)"
echo "  Start:     \"$VENV_PY\" \"$TARGET/main.py\""
echo "  URL:       http://127.0.0.1:8188  (override with KAIZEN_COMFYUI_URL or --endpoint)"
echo "  Verify:    python kaizen.py Y5 --json"
echo "  Workspace: add $TARGET to your local .code-workspace (stays out of git)"
