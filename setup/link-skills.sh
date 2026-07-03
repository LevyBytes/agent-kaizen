#!/usr/bin/env bash
# ============================================================================
#  link-skills.sh  --  populate the SKILLS store and link skills (Linux/macOS)
# ----------------------------------------------------------------------------
#  The public repo ships with NO skills. This helper optionally clones a skills
#  store into $DEVROOT/SKILLS, then creates a per-skill SYMLINK for every skill
#  (a folder containing SKILL.md) under $DEVROOT/SKILLS/skills/<name> into BOTH
#  .agents/skills/<name> and .claude/skills/<name>. Re-running is safe.
#
#  Usage:
#    bash setup/link-skills.sh [skills-store-git-url]
#
#  Expected store layout: $DEVROOT/SKILLS/skills/<skill-name>/SKILL.md
#  Public repo: https://github.com/LevyBytes/agent-kaizen
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STORE_URL="${1:-}"
DEVROOT="${DEVROOT:-$(cd "$REPO_ROOT/.." && pwd)}"
STORE="$DEVROOT/SKILLS"
SKILLS_DIR="$STORE/skills"

echo "=== Agent Kaizen: link skills ==="
echo "  DEVROOT : $DEVROOT"
echo "  store   : $STORE"

# 1. Optionally clone or update the skills store.
if [ -n "$STORE_URL" ]; then
  if [ -d "$STORE/.git" ]; then
    echo "  Updating existing store (git pull)..."
    git -C "$STORE" pull --ff-only || echo "  [warn] git pull did not fast-forward; leaving store as-is."
  else
    skills_real="$([ -d "$SKILLS_DIR" ] && ls -A "$SKILLS_DIR" 2>/dev/null | grep -v '^\.gitkeep$' || true)"
    root_real="$([ -d "$STORE" ] && ls -A "$STORE" 2>/dev/null | grep -vE '^(\.gitkeep|skills|\.git)$' || true)"
    if [ -n "$skills_real" ] || [ -n "$root_real" ]; then
      echo "ERROR: SKILLS already has content at $STORE; manage it manually."; exit 1
    fi
    [ -e "$STORE" ] && rm -rf "$STORE"
    git clone "$STORE_URL" "$STORE"
  fi
fi

[ -d "$SKILLS_DIR" ] || { echo "ERROR: no skills at $SKILLS_DIR; pass a store URL or populate it first."; exit 1; }

# 2. Link every skill (a folder containing SKILL.md) into both mirrors.
linked=0
for d in "$SKILLS_DIR"/*/; do
  [ -f "${d}SKILL.md" ] || continue
  name="$(basename "$d")"
  for m in "$REPO_ROOT/.agents/skills" "$REPO_ROOT/.claude/skills"; do
    mkdir -p "$m"
    [ -e "$m/$name" ] || ln -s "${d%/}" "$m/$name"
  done
  linked=$((linked + 1))
  echo "  linked: $name"
done
echo "  $linked skill(s) linked into .agents/skills and .claude/skills."

# 3. Best-effort: regenerate INDEX.md if the store carries skill-drafting.
BUILDER="$SKILLS_DIR/skill-drafting/scripts/skill_builder.py"
if [ -f "$BUILDER" ]; then
  PY="$DEVROOT/Python/venvs/kaizen/bin/python"
  [ -x "$PY" ] || PY="python3"
  "$PY" "$BUILDER" index "$REPO_ROOT/.claude/skills" --mirror "$REPO_ROOT/.agents/skills" || true
else
  echo "  (INDEX.md not regenerated: skill-drafting/scripts/skill_builder.py not found in the store.)"
fi
