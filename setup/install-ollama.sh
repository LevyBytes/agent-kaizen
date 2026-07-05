#!/usr/bin/env bash
# Light setup for the Ollama model backend (B* / model-*).
set -euo pipefail

EMBED_MODEL="nomic-embed-text"
CHAT_MODEL="llama3.2"
DEVROOT_ARG=""
LIST_STEPS=0
EMIT_PLAN_JSON=""

while [ $# -gt 0 ]; do
  case "$1" in
    --embed-model) EMBED_MODEL="${2:-}"; shift ;;
    --chat-model) CHAT_MODEL="${2:-}"; shift ;;
    --devroot) DEVROOT_ARG="${2:-}"; shift ;;
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
ak_init "Agent Kaizen Ollama backend installer" "$REPO_ROOT" "$DEVROOT_RESOLVED" \
  "$(ak_step_obj preflight 'Resolve DEVROOT, model store, and ollama command')" \
  "$(ak_step_obj embed 'Pull embedding model')" \
  "$(ak_step_obj chat 'Pull chat model')" \
  "$(ak_step_obj summary 'Print backend environment and verification command')"

if [ "$LIST_STEPS" -eq 1 ] || [ "$AK_PLAN_ONLY" -eq 1 ]; then
  ak_show_plan
  [ -z "$EMIT_PLAN_JSON" ] || ak_write_plan_json "$EMIT_PLAN_JSON"
  exit 0
fi
[ "$AK_SELF_TEST" -eq 0 ] || ak_show_plan

MODELS="$DEVROOT_RESOLVED/Ollama/models"
OLLAMA=""

step_preflight() {
  mkdir -p "$MODELS"
  export OLLAMA_MODELS="$MODELS"
  OLLAMA="$(command -v ollama || true)"
  [ -n "$OLLAMA" ] || ak_die "Ollama is not on PATH. Install it from https://ollama.com/download, then re-run this script."
  printf 'Model store: %s\n' "$MODELS"
  printf 'Ollama: %s\n' "$OLLAMA"
}

step_embed() {
  ak_run --note "Ollama is downloading/verifying model data for $EMBED_MODEL." -- "$OLLAMA" pull "$EMBED_MODEL"
}

step_chat() {
  ak_run --note "Ollama is downloading/verifying model data for $CHAT_MODEL." -- "$OLLAMA" pull "$CHAT_MODEL"
}

step_summary() {
  printf '\nOllama backend ready. Current-shell settings:\n'
  printf '  export OLLAMA_MODELS="%s"\n' "$MODELS"
  printf '  export KAIZEN_EMBED_MODEL=%s\n' "$EMBED_MODEL"
  printf '  export KAIZEN_LLM_MODEL=%s\n' "$CHAT_MODEL"
  printf '  Verify: python kaizen.py B1 --json\n'
}

ak_run_step preflight 'Resolve DEVROOT, model store, and ollama command' step_preflight
ak_run_step embed 'Pull embedding model' step_embed
ak_run_step chat 'Pull chat model' step_chat
ak_run_step summary 'Print backend environment and verification command' step_summary
