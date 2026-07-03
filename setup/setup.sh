#!/usr/bin/env bash
# ============================================================================
#  setup.sh  --  Agent Kaizen setup for Linux / macOS (clone-first)
# ----------------------------------------------------------------------------
#  Run this AFTER cloning the repo. It builds the shared Python venv, installs
#  the pinned dependencies, scaffolds an empty sibling SKILLS store, generates a
#  VS Code workspace, and initializes the local Kaizen DB (no guardrails seeded).
#
#  Usage:
#    bash setup/setup.sh [DEVROOT]
#      DEVROOT  parent folder that holds this repo, the SKILLS store, and the
#               shared venv. Defaults to $DEVROOT, else this repo's parent.
#
#  Public repo: https://github.com/LevyBytes/agent-kaizen
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEVROOT="${1:-${DEVROOT:-$(cd "$REPO_ROOT/.." && pwd)}}"

echo "=== Agent Kaizen setup (Linux/macOS) ==="
echo "  repo    : $REPO_ROOT"
echo "  DEVROOT : $DEVROOT"

# 1. Prerequisites -----------------------------------------------------------
command -v git >/dev/null 2>&1 || { echo "ERROR: git not found on PATH."; exit 1; }
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "ERROR: python3 not found (3.10+ required)."; exit 1; }
PYVER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
echo "  python  : $PYVER"

# 2. Shared venv -------------------------------------------------------------
VENV="$DEVROOT/Python/venvs/kaizen"
VENV_PY="$VENV/bin/python"
if [ ! -x "$VENV_PY" ]; then
  mkdir -p "$DEVROOT/Python/venvs"
  "$PY" -m venv "$VENV"
fi
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -r "$REPO_ROOT/requirements-kaizen.txt"
echo "  venv    : $VENV"

# 3. Empty skills store ------------------------------------------------------
mkdir -p "$DEVROOT/SKILLS/skills"
if [ ! -f "$DEVROOT/SKILLS/README.md" ]; then
  printf '# SKILLS store\n\nEmpty by default. Use the repo'\''s setup/link-skills.sh to clone skill repos here and link them into .agents/skills + .claude/skills.\n' > "$DEVROOT/SKILLS/README.md"
fi
echo "  skills  : $DEVROOT/SKILLS/skills (empty)"

# 4. VS Code workspace (minimal; Linux interpreter path) ---------------------
WS_DIR="$REPO_ROOT/_workspace"
mkdir -p "$WS_DIR"
cat > "$WS_DIR/agent-kaizen-tools.code-workspace" <<'JSON'
{
  "folders": [
    { "name": "Agent Kaizen", "path": ".." },
    { "name": "SKILLS", "path": "../../SKILLS" }
  ],
  "settings": {
    "python.defaultInterpreterPath": "${workspaceFolder:Agent Kaizen}/../Python/venvs/kaizen/bin/python",
    "files.exclude": { "**/__pycache__": true },
    "search.exclude": { "**/__pycache__": true }
  }
}
JSON
echo "  vscode  : $WS_DIR/agent-kaizen-tools.code-workspace"

# 5. Local DB (init only - NO policy/guardrail seeding) ----------------------
"$VENV_PY" "$REPO_ROOT/kaizen.py" K1 --json >/dev/null
echo "  db      : initialized (empty policy DB)"

# 6. Done --------------------------------------------------------------------
echo ""
echo "Agent Kaizen is ready."
echo "  Persist DEVROOT in your shell profile if you like:"
echo "    export DEVROOT=\"$DEVROOT\""
echo "  Open it:"
echo "    code \"$WS_DIR/agent-kaizen-tools.code-workspace\""
echo "  Skills (optional):"
echo "    bash setup/link-skills.sh <skills-store-git-url>"
