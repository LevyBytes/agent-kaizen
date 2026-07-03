#!/usr/bin/env bash
# Light setup for the Ollama model backend (B* / model-*).
#
# Ollama installs via its own official installer (https://ollama.com/download); this script verifies
# it is present, relocates the model store under $DEVROOT (so weights stay out of the repo), pulls an
# embedding + chat model, and prints the env vars Kaizen reads. Run it yourself.
#
# Usage: bash setup/install-ollama.sh [--embed-model nomic-embed-text] [--chat-model llama3.2] [--devroot DIR]
set -euo pipefail

EMBED_MODEL="nomic-embed-text"
CHAT_MODEL="llama3.2"
DEVROOT_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --embed-model) EMBED_MODEL="${2:-}"; shift ;;
    --chat-model) CHAT_MODEL="${2:-}"; shift ;;
    --devroot) DEVROOT_ARG="${2:-}"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '==> %s\n' "$1"; }
warn() { printf '  ! %s\n' "$1" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DEVROOT="${DEVROOT_ARG:-${DEVROOT:-$(dirname "$REPO_ROOT")}}"
MODELS="$DEVROOT/Ollama/models"
mkdir -p "$MODELS"

if ! command -v ollama >/dev/null 2>&1; then
  warn "Ollama is not on PATH. Install it from https://ollama.com/download, then re-run this script."
  exit 1
fi

step "Model store: $MODELS"
export OLLAMA_MODELS="$MODELS"
step "Pulling embedding model: $EMBED_MODEL"
ollama pull "$EMBED_MODEL" || warn "embed-model pull failed (is the Ollama service running?)."
step "Pulling chat model: $CHAT_MODEL"
ollama pull "$CHAT_MODEL" || warn "chat-model pull failed (is the Ollama service running?)."

echo ""
step "Ollama backend ready. Point Kaizen at the models (current shell):"
echo "  export OLLAMA_MODELS=\"$MODELS\""
echo "  export KAIZEN_EMBED_MODEL=$EMBED_MODEL    # enables E3 embeddings + E4 --semantic"
echo "  export KAIZEN_LLM_MODEL=$CHAT_MODEL       # enables B2 model-run"
echo "  # remote OpenAI-compatible endpoint: set KAIZEN_EMBED_BASE_URL / KAIZEN_LLM_BASE_URL (+ *_API_KEY, env only)"
echo "  Verify: python kaizen.py B1 --json"
