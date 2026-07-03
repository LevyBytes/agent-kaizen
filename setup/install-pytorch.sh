#!/usr/bin/env bash
# Install the OPT-IN PyTorch extra (in-process sentence-transformers embeddings + semantic chunking).
#
# Embeddings run IN-PROCESS, so this installs into the python on PATH -- run it with the SAME python
# (venv) you launch kaizen.py with. Redirects the HuggingFace weight cache to $DEVROOT/models via
# HF_HOME so weights stay out of the repo. torch is heavy + GPU-specific (use --gpu for a CUDA wheel).
#
# Usage: bash setup/install-pytorch.sh [--gpu] [--cuda-index URL] [--devroot DIR]
set -euo pipefail

GPU=0
CUDA_INDEX="https://download.pytorch.org/whl/cu121"
DEVROOT_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --gpu) GPU=1 ;;
    --cuda-index) CUDA_INDEX="${2:-}"; shift ;;
    --devroot) DEVROOT_ARG="${2:-}"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '==> %s\n' "$1"; }
die() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DEVROOT="${DEVROOT_ARG:-${DEVROOT:-$(dirname "$REPO_ROOT")}}"
CACHE="$DEVROOT/models"
mkdir -p "$CACHE"
export HF_HOME="$CACHE"

PYTHON="$(command -v python3 || command -v python || true)"
[ -n "$PYTHON" ] || die "python not found on PATH."
step "Installing into: $PYTHON  (must be the venv you run kaizen.py with)"
step "HF weight cache: $CACHE  (HF_HOME)"

if [ "$GPU" -eq 1 ]; then
  step "Installing CUDA torch from $CUDA_INDEX ..."
  "$PYTHON" -m pip install torch --index-url "$CUDA_INDEX"
fi
step "Installing the opt-in extra (sentence-transformers + torch) ..."
"$PYTHON" -m pip install -r "$REPO_ROOT/requirements-pytorch.txt"

echo ""
step "PyTorch embedding backend installed. Point Kaizen at it (current shell):"
echo "  export HF_HOME=\"$CACHE\""
echo "  export KAIZEN_EMBED_BACKEND=sentence-transformers"
echo "  export KAIZEN_EMBED_MODEL=all-MiniLM-L6-v2    # optional; this is the default"
echo "  Verify: python kaizen.py B1 --json"
